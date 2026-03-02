"""
颜色数据管理模块
负责加载和管理颜色数据
"""
from pathlib import Path
from typing import Any, Optional

from core.plugin_base import load_json, write_json, ensure_dir

def load_colors(context) -> list[dict[str, Any]]:
    """加载所有颜色数据
    
    Args:
        context: 插件上下文
        
    Returns:
        颜色数据列表
    """
    colors = []
    
    # 加载内置颜色库
    try:
        builtin_file = context.plugin_dir / "color.json"
        if builtin_file.exists():
            builtin_colors = load_json(builtin_file, [])
            if isinstance(builtin_colors, dict):
                builtin_colors = [builtin_colors]
            colors.extend(builtin_colors)
            context.logger.debug(f"加载内置颜色库: {len(builtin_colors)} 个颜色")
        else:
            context.logger.warning(f"内置颜色库文件不存在: {builtin_file}")
    except Exception as exc:
        context.logger.error(f"加载内置颜色库失败: {exc}", exc_info=True)
    
    # 加载用户自定义颜色
    try:
        custom_file = context.data_dir / "custom_colors.json"
        if custom_file.exists():
            custom_colors = load_json(custom_file, [])
            if isinstance(custom_colors, dict):
                custom_colors = [custom_colors]
            colors.extend(custom_colors)
            context.logger.debug(f"加载自定义颜色: {len(custom_colors)} 个")
    except Exception as exc:
        context.logger.error(f"加载自定义颜色失败: {exc}", exc_info=True)
    
    return colors

def load_custom_colors(context) -> list[dict[str, Any]]:
    """加载用户自定义颜色
    
    Args:
        context: 插件上下文
        
    Returns:
        自定义颜色列表
    """
    custom_file = context.data_dir / "custom_colors.json"
    return load_json(custom_file, [])

def save_custom_colors(colors: list[dict[str, Any]], context) -> None:
    """保存用户自定义颜色
    
    Args:
        colors: 颜色列表
        context: 插件上下文
    """
    custom_file = context.data_dir / "custom_colors.json"
    ensure_dir(context.data_dir)
    write_json(custom_file, colors)

def get_color_systems(colors: list[dict[str, Any]]) -> set:
    """获取所有颜色色系
    
    Args:
        colors: 颜色列表
        
    Returns:
        色系集合
    """
    color_systems = set()
    for c in colors:
        name = c.get('name', '')
        if name:
            last_char = name[-1]
            if last_char == '色' and len(name) > 1:
                color_systems.add(name[-2])
            else:
                color_systems.add(last_char)
    return color_systems

def format_color_info(color: dict) -> str:
    """格式化颜色信息
    
    Args:
        color: 颜色数据字典
        
    Returns:
        格式化的颜色信息字符串
    """
    lines = []
    for key in ['name', 'RGB', 'hex', 'CMYK']:
        if key in color and key != 'pinyin':
            lines.append(f"{key}: {color[key]}")
    return '\n'.join(lines)
