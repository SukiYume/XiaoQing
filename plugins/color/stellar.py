"""
恒星光谱颜色模块
提供恒星光谱型颜色查询和列举功能
"""
import importlib
from functools import lru_cache
from pathlib import Path
from typing import Any

from core.plugin_base import image, segments, text
from core.public_errors import public_error_response

from .convert import hex_to_rgb
from .image_gen import generate_color_image

# 检查可选依赖
try:
    pd = importlib.import_module("pandas")
    PANDAS_AVAILABLE = True
except ImportError:
    PANDAS_AVAILABLE = False

MAX_SPECTRAL_TYPES = 30


@lru_cache(maxsize=4)
def _load_stellar_dataframe(stellar_file: str, mtime_ns: int):
    import pandas as pd

    return pd.read_csv(Path(stellar_file), sep=r"\s+")

def load_stellar_colors(context) -> Any | None:
    """加载恒星光谱颜色数据

    Args:
        context: 插件上下文

    Returns:
        pandas DataFrame 或 None
    """
    if not PANDAS_AVAILABLE:
        context.logger.warning("恒星颜色功能不可用：缺少 pandas 依赖")
        return None

    stellar_file = context.plugin_dir / "stellar_colors.txt"
    if not stellar_file.exists():
        context.logger.warning(f"恒星颜色数据文件不存在: {stellar_file}")
        return None

    df = _load_stellar_dataframe(str(stellar_file), stellar_file.stat().st_mtime_ns)
    context.logger.debug("加载恒星颜色数据: %s 条", len(df))
    return df

async def query_stellar_color(spec_type: str, context, img_dir: Path) -> list[dict[str, Any]]:
    """查询恒星光谱颜色

    Args:
        spec_type: 光谱型
        context: 插件上下文
        img_dir: 图片输出目录

    Returns:
        消息段列表
    """
    if not PANDAS_AVAILABLE:
        return segments("❌ 恒星颜色查询功能不可用\n需要安装 pandas 依赖：pip install pandas")

    try:
        df = load_stellar_colors(context)
        if df is None:
            return segments("❌ 恒星颜色数据文件不存在")

        match = df[df['SpT'] == spec_type]
        if match.empty:
            return segments(f"❌ 没有找到光谱型「{spec_type}」的恒星颜色\n\n提示：使用 /color -t 查看可用的光谱型")

        row = match.iloc[0]
        hex_value = row.get('Hex', '#FFFFFF')
        rgb = hex_to_rgb(hex_value)

        info = f"🌟 恒星光谱颜色\n\n光谱型: {spec_type}\nHEX: {hex_value}\nRGB: {rgb}"
        img_path = await generate_color_image(spec_type, rgb, img_dir, context)

        context.logger.info(f"查询恒星颜色: {spec_type}")

        result = [text(info)]
        if img_path:
            result.append(image(img_path))
        return result

    except Exception as exc:
        return public_error_response(
            context, exc, logger=context.logger, component="color.stellar.query"
        )

def list_spectral_types(prefix: str, context) -> list[dict[str, Any]]:
    """列出符合前缀的光谱型

    Args:
        prefix: 光谱型前缀
        context: 插件上下文

    Returns:
        消息段列表
    """
    if not PANDAS_AVAILABLE:
        return segments("❌ 光谱型查询功能不可用\n需要安装 pandas 依赖：pip install pandas")

    try:
        df = load_stellar_colors(context)
        if df is None:
            return segments("❌ 恒星颜色数据文件不存在")

        if prefix:
            matches = df[df['SpT'].str.contains(prefix, case=False)]
            if matches.empty:
                return segments(f"❌ 没有找到包含「{prefix}」的光谱型")
            types = matches['SpT'].tolist()
            title = f"包含「{prefix}」的光谱型（共 {len(types)} 个）："
        else:
            types = df['SpT'].tolist()
            title = f"所有光谱型（共 {len(types)} 个）："

        # 限制显示数量
        display_types = types[:MAX_SPECTRAL_TYPES]
        suffix = f"\n\n... 还有 {len(types) - MAX_SPECTRAL_TYPES} 个" if len(types) > MAX_SPECTRAL_TYPES else ""

        context.logger.info(f"列出光谱型: prefix={prefix}, count={len(types)}")
        return segments(title + "\n" + ", ".join(display_types) + suffix)

    except Exception as exc:
        return public_error_response(
            context, exc, logger=context.logger, component="color.stellar.list"
        )
