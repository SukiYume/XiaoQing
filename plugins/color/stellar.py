"""读取小型恒星色表并提供光谱型查询，无需 DataFrame 依赖。"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, cast

from core.args import quote_token
from core.interfaces import PluginContextProtocol
from core.plugin_base import has_control_characters, image, segments, text
from core.public_errors import public_error_response

from .convert import hex_to_rgb
from .image_gen import generate_color_image

MAX_STELLAR_FILE_BYTES = 64 * 1024
MAX_STELLAR_ROWS       = 512
MAX_STELLAR_LINE_CHARS = 256
MAX_SPECTRAL_TYPES     = 30
_STELLAR_HEADER        = ("SpT", "Teff", "log(g)", "RGB", "Hex")
_SPECTRAL_TYPE_PATTERN = re.compile(r"[OBAFGKM][0-9](?:\.5)?V", re.IGNORECASE)
_HEX_PATTERN           = re.compile(r"#[0-9a-fA-F]{6}")
Messages               = list[dict[str, Any]]


@dataclass(frozen=True)
class StellarColor:
    spectral_type: str
    temperature_k: int
    log_g: float
    linear_rgb: tuple[float, float, float]
    hex_value: str


def _parse_stellar_row(line: str, *, line_number: int) -> StellarColor:
    if not line or len(line) > MAX_STELLAR_LINE_CHARS:
        raise ValueError(f"invalid stellar row length at line {line_number}")
    parts = line.split()
    if len(parts) != len(_STELLAR_HEADER):
        raise ValueError(f"invalid stellar column count at line {line_number}")
    spectral_type, raw_temperature, raw_log_g, raw_rgb, hex_value = parts
    if _SPECTRAL_TYPE_PATTERN.fullmatch(spectral_type) is None:
        raise ValueError(f"invalid spectral type at line {line_number}")
    try:
        temperature = int(raw_temperature)
        log_g       = float(raw_log_g)
        rgb_values  = tuple(float(value) for value in raw_rgb.split(","))
    except ValueError as exc:
        raise ValueError(f"invalid stellar numeric value at line {line_number}") from exc
    if not 1_000 <= temperature <= 100_000 or not math.isfinite(log_g) or not 0 <= log_g <= 10:
        raise ValueError(f"stellar parameter out of range at line {line_number}")
    if len(rgb_values) != 3 or any(
        not math.isfinite(value) or not 0 <= value <= 1 for value in rgb_values
    ):
        raise ValueError(f"invalid linear RGB at line {line_number}")
    if _HEX_PATTERN.fullmatch(hex_value) is None:
        raise ValueError(f"invalid stellar HEX value at line {line_number}")
    # 论文表格的线性 RGB 只保留三位小数，HEX 来自更高精度原值，因此不反推二者相等。
    return StellarColor(
        spectral_type = spectral_type.upper(),
        temperature_k = temperature,
        log_g         = log_g,
        linear_rgb    = rgb_values,
        hex_value     = hex_value.casefold(),
    )


@lru_cache(maxsize=4)
def _load_stellar_rows_cached(
    stellar_file: str,
    mtime_ns: int,
    size: int,
) -> tuple[StellarColor, ...]:
    del mtime_ns, size  # 两项只参与缓存身份。
    path = Path(stellar_file)
    if path.is_symlink() or not path.is_file():
        raise ValueError("stellar color path must be a regular non-link file")
    if path.stat().st_size > MAX_STELLAR_FILE_BYTES:
        raise ValueError("stellar color file exceeds byte budget")
    content = path.read_text(encoding="utf-8")
    if len(content.encode("utf-8")) > MAX_STELLAR_FILE_BYTES:
        raise ValueError("stellar color file changed while reading")
    lines = content.splitlines()
    if not lines or tuple(lines[0].split()) != _STELLAR_HEADER:
        raise ValueError("stellar color header is invalid")
    if not 1 <= len(lines) - 1 <= MAX_STELLAR_ROWS:
        raise ValueError("stellar color row count is invalid")
    return tuple(
        _parse_stellar_row(line, line_number=line_number)
        for line_number, line in enumerate(lines[1:], start=2)
    )


def load_stellar_colors(context: PluginContextProtocol) -> tuple[StellarColor, ...]:
    plugin_dir = getattr(context, "plugin_dir", None)
    if not isinstance(plugin_dir, Path):
        raise ValueError("color plugin_dir must be a Path")
    path = plugin_dir / "stellar_colors.txt"
    info = path.stat()
    rows = _load_stellar_rows_cached(str(path), info.st_mtime_ns, info.st_size)
    context.logger.debug("加载恒星颜色数据: count=%d", len(rows))
    return rows


def _clean_spectral_term(value: str, *, allow_empty: bool) -> str:
    if not isinstance(value, str):
        raise ValueError("光谱型必须是字符串")
    cleaned = value.strip().upper()
    if (not cleaned and not allow_empty) or len(cleaned) > 16:
        raise ValueError("光谱型为空或超过长度上限")
    if has_control_characters(cleaned):
        raise ValueError("光谱型包含控制字符")
    return cleaned


def is_spectral_type(value: str) -> bool:
    """判断直接查询是否是一个完整、受支持形态的主序星光谱型。"""

    if not isinstance(value, str):
        return False
    return _SPECTRAL_TYPE_PATTERN.fullmatch(value.strip()) is not None


async def query_stellar_color(
    spec_type: str,
    context: PluginContextProtocol,
    image_dir: Path,
) -> Messages:
    """查询光谱型；重复类型保持论文表顺序并采用第一条温度采样。"""

    try:
        normalized = _clean_spectral_term(spec_type, allow_empty=False)
        matches = [row for row in load_stellar_colors(context) if row.spectral_type == normalized]
        if not matches:
            return cast(
                Messages,
                segments(
                    f"❌ 没有找到光谱型「{normalized}」的恒星颜色\n\n"
                    "提示：使用 /color stars 查看可用的光谱型"
                ),
            )

        selected    = matches[0]
        rgb         = hex_to_rgb(selected.hex_value)
        sample_note = ""
        if len(matches) > 1:
            sample_note = (
                f"\n温度采样：{matches[0].temperature_k:,}-"
                f"{matches[-1].temperature_k:,} K（展示第一条）"
            )
        info = (
            "🌟 恒星光谱颜色\n\n"
            f"光谱型：{selected.spectral_type}\n"
            f"有效温度：{selected.temperature_k:,} K\n"
            f"HEX：{selected.hex_value}\n"
            f"RGB：{', '.join(str(channel) for channel in rgb)}{sample_note}"
        )
        image_path = await generate_color_image(selected.spectral_type, rgb, image_dir, context)
        context.logger.info("查询恒星颜色: matches=%d", len(matches))
        result = [text(info)]
        if image_path:
            result.append(image(image_path))
        return result
    except ValueError as exc:
        return cast(Messages, segments(f"❌ {exc}"))
    except Exception as exc:
        return cast(
            Messages,
            public_error_response(
                context, exc, logger=context.logger, component="color.stellar.query"
            ),
        )


def list_spectral_types(
    prefix: str,
    context: PluginContextProtocol,
    *,
    page: int = 1,
) -> Messages:
    """按字面子串筛选并列出不重复的光谱型。"""

    try:
        if type(page) is not int or page < 1:
            raise ValueError("光谱型页码必须是正整数")
        normalized_prefix = _clean_spectral_term(prefix, allow_empty=True)
        unique_types = list(
            dict.fromkeys(row.spectral_type for row in load_stellar_colors(context))
        )
        matches = [
            spectral_type for spectral_type in unique_types if normalized_prefix in spectral_type
        ]
        if not matches:
            return cast(
                Messages,
                segments(f"❌ 没有找到包含「{normalized_prefix}」的光谱型"),
            )

        title = (
            f"包含「{normalized_prefix}」的光谱型（共 {len(matches)} 个）："
            if normalized_prefix
            else f"所有光谱型（共 {len(matches)} 个）："
        )
        total_pages = (len(matches) + MAX_SPECTRAL_TYPES - 1) // MAX_SPECTRAL_TYPES
        if page > total_pages:
            return cast(Messages, segments(f"❌ 第 {page} 页超出范围（共 {total_pages} 页）"))
        start                 = (page - 1) * MAX_SPECTRAL_TYPES
        displayed             = matches[start : start + MAX_SPECTRAL_TYPES]
        query_part            = f" {quote_token(normalized_prefix)}" if normalized_prefix else ""
        navigation: list[str] = []
        if page > 1:
            navigation.append(f"上一页：/color stars{query_part} --page {page - 1}")
        if page < total_pages:
            navigation.append(f"下一页：/color stars{query_part} --page {page + 1}")
        suffix = f"\n\n第 {page}/{total_pages} 页"
        if navigation:
            suffix += "\n" + "\n".join(navigation)
        context.logger.info(
            "列出光谱型: prefix_chars=%d page=%d count=%d",
            len(normalized_prefix),
            page,
            len(matches),
        )
        return cast(Messages, segments(f"{title}\n{', '.join(displayed)}{suffix}"))
    except ValueError as exc:
        return cast(Messages, segments(f"❌ {exc}"))
    except Exception as exc:
        return cast(
            Messages,
            public_error_response(
                context, exc, logger=context.logger, component="color.stellar.list"
            ),
        )
