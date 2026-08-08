"""异步生成并有界缓存颜色预览 PNG。"""

from __future__ import annotations

import hashlib
import importlib
import io
import logging
from functools import lru_cache
from importlib.util import find_spec
from pathlib import Path
from typing import Any, cast

from core.bounded_file_cache import BoundedFileCache, FileCacheLimits
from core.interfaces import PluginContextProtocol
from core.plugin_base import has_control_characters, run_sync
from core.public_errors import public_error_message

from .convert import validate_rgb

MAX_IMAGE_NAME_CHARS = 64
IMAGE_CACHE_LIMITS = FileCacheLimits(
    max_entries=256,
    max_bytes=32 * 1024 * 1024,
    ttl_seconds=30 * 24 * 60 * 60,
)

MATPLOTLIB_AVAILABLE = find_spec("numpy") is not None and find_spec("matplotlib") is not None
_CJK_FONT_FAMILIES = (
    "Noto Sans CJK SC",
    "Noto Sans CJK",
    "WenQuanYi Zen Hei",
    "Microsoft YaHei",
    "SimHei",
)
logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _renderer_modules() -> tuple[Any, Any, Any]:
    """首次真正生成色卡时才加载两个体积较大的绘图库。"""

    numpy = importlib.import_module("numpy")
    matplotlib = importlib.import_module("matplotlib")
    matplotlib.use("Agg")
    pyplot = importlib.import_module("matplotlib.pyplot")
    font_manager = importlib.import_module("matplotlib.font_manager")
    return numpy, pyplot, font_manager


def _find_cjk_font(font_manager: Any) -> Any | None:
    for family in _CJK_FONT_FAMILIES:
        try:
            path = font_manager.findfont(family, fallback_to_default=False)
        except (OSError, ValueError):
            continue
        return font_manager.FontProperties(fname=path)
    logger.debug("未找到已知 CJK 字体，颜色名使用 matplotlib 默认字体")
    return None


def _clean_image_input(name: str, rgb: list[int]) -> tuple[str, list[int]]:
    if not isinstance(name, str):
        raise ValueError("color image name must be a string")
    cleaned_name = name.strip()
    if not cleaned_name or len(cleaned_name) > MAX_IMAGE_NAME_CHARS:
        raise ValueError("color image name is empty or too long")
    if has_control_characters(cleaned_name):
        raise ValueError("color image name contains control characters")
    valid, error = validate_rgb(rgb)
    if not valid:
        raise ValueError(error or "invalid RGB image color")
    return cleaned_name, cast(list[int], list(rgb))


def _cache_filename(name: str, rgb: list[int]) -> str:
    identity = f"{name}\0{','.join(str(value) for value in rgb)}"
    return f"{hashlib.sha256(identity.encode('utf-8')).hexdigest()}.png"


def _render_color_image(name: str, rgb: list[int]) -> bytes:
    if not MATPLOTLIB_AVAILABLE:
        raise RuntimeError("matplotlib renderer is unavailable")
    numpy, pyplot, font_manager = _renderer_modules()
    image_array: Any = numpy.zeros([100, 200, 3], numpy.uint8)
    image_array[:] = rgb
    figure, axes = pyplot.subplots(figsize=(2, 1))
    try:
        axes.imshow(image_array)
        axes.axis("off")
        font_properties = _find_cjk_font(font_manager)
        if font_properties is None:
            axes.set_title(name, fontsize=10)
        else:
            axes.set_title(name, fontsize=10, fontproperties=font_properties)
        output = io.BytesIO()
        figure.savefig(output, format="png", dpi=72, bbox_inches="tight", pad_inches=0.1)
        return output.getvalue()
    finally:
        pyplot.close(figure)


async def generate_color_image(
    name: str,
    rgb: list[int],
    output_dir: Path,
    context: PluginContextProtocol,
) -> str | None:
    """生成内容寻址色卡；依赖缺失或生成失败时返回 ``None``。"""

    if not MATPLOTLIB_AVAILABLE:
        context.logger.warning(
            "图片生成失败：缺少 matplotlib 或 numpy 依赖；安装依赖后请执行 /reload"
        )
        return None

    try:
        cleaned_name, cleaned_rgb = _clean_image_input(name, rgb)
        if not isinstance(output_dir, Path):
            raise ValueError("color image output_dir must be a Path")
        cache = BoundedFileCache(output_dir, IMAGE_CACHE_LIMITS)
        filename = _cache_filename(cleaned_name, cleaned_rgb)
        cached = await run_sync(cache.get_any, (filename,))
        if cached is not None:
            return str(cached)

        payload = await run_sync(_render_color_image, cleaned_name, cleaned_rgb)
        image_path = await run_sync(cache.put, filename, payload)
        if image_path is None:
            context.logger.warning("颜色图片超过缓存总字节预算")
            return None
        context.logger.debug("生成颜色图片: bytes=%d", len(payload))
        return str(image_path)
    except Exception as exc:
        public_error_message(context, exc, logger=context.logger, component="color.generate_image")
        return None
