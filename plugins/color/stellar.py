"""
恒星光谱颜色模块
提供恒星光谱型颜色查询和列举功能
"""
from pathlib import Path
from typing import Any, Optional

from core.plugin_base import segments, text, image
from .convert import hex_to_rgb
from .image_gen import generate_color_image

# 检查可选依赖
try:
    import pandas as pd
    PANDAS_AVAILABLE = True
except ImportError:
    PANDAS_AVAILABLE = False

MAX_SPECTRAL_TYPES = 30

def load_stellar_colors(context) -> Optional[Any]:
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
    
    try:
        import pandas as pd
        df = pd.read_csv(stellar_file, sep=r'\s+')
        context.logger.debug(f"加载恒星颜色数据: {len(df)} 条")
        return df
    except Exception as exc:
        context.logger.error(f"加载恒星颜色数据失败: {exc}", exc_info=True)
        return None

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
    
    stellar_file = context.plugin_dir / "stellar_colors.txt"
    
    if not stellar_file.exists():
        return segments("❌ 恒星颜色数据文件不存在")
    
    try:
        import pandas as pd
        df = pd.read_csv(stellar_file, sep=r'\s+')
        
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
        context.logger.error(f"查询恒星颜色失败: {spec_type}, 错误: {exc}", exc_info=True)
        return segments(f"❌ 查询恒星颜色时出错: {exc}")

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
    
    stellar_file = context.plugin_dir / "stellar_colors.txt"
    
    if not stellar_file.exists():
        return segments("❌ 恒星颜色数据文件不存在")
    
    try:
        import pandas as pd
        df = pd.read_csv(stellar_file, sep=r'\s+')
        
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
        context.logger.error(f"查询光谱型失败: prefix={prefix}, 错误: {exc}", exc_info=True)
        return segments(f"❌ 查询光谱型时出错: {exc}")
