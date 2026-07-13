"""
颜色数据管理模块
负责加载和管理颜色数据
"""
import threading
from functools import lru_cache
from pathlib import Path
from typing import Any

from core.plugin_base import ensure_dir, load_json, write_json
from core.public_errors import public_error_message

_CUSTOM_COLORS_LOCK = threading.RLock()


def _custom_file(context) -> Path:
    group_id = getattr(context, "current_group_id", None)
    if group_id is not None:
        scope = f"group_{int(group_id)}"
    else:
        user_id = getattr(context, "current_user_id", None)
        scope = f"private_{int(user_id)}" if user_id is not None else "legacy"
    name = "custom_colors.json" if scope == "legacy" else f"custom_colors_{scope}.json"
    return context.data_dir / name


@lru_cache(maxsize=4)
def _load_builtin_colors_cached(color_file: str, mtime_ns: int) -> list[dict[str, Any]]:
    builtin_colors = load_json(Path(color_file), [])
    if isinstance(builtin_colors, dict):
        return [builtin_colors]
    if isinstance(builtin_colors, list):
        return builtin_colors
    return []

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
            builtin_colors = _load_builtin_colors_cached(
                str(builtin_file),
                builtin_file.stat().st_mtime_ns,
            )
            colors.extend(builtin_colors)
            context.logger.debug(f"加载内置颜色库: {len(builtin_colors)} 个颜色")
        else:
            context.logger.warning(f"内置颜色库文件不存在: {builtin_file}")
    except Exception as exc:
        public_error_message(
            context,
            exc,
            logger=context.logger,
            component="color.load_builtin",
        )

    # 加载用户自定义颜色
    try:
        custom_file = _custom_file(context)
        if custom_file.exists():
            custom_colors = load_json(custom_file, [])
            if isinstance(custom_colors, dict):
                custom_colors = [custom_colors]
            colors.extend(custom_colors)
            context.logger.debug(f"加载自定义颜色: {len(custom_colors)} 个")
    except Exception as exc:
        public_error_message(
            context,
            exc,
            logger=context.logger,
            component="color.load_custom",
        )

    return colors

def load_custom_colors(context) -> list[dict[str, Any]]:
    """加载用户自定义颜色

    Args:
        context: 插件上下文

    Returns:
        自定义颜色列表
    """
    custom_file = _custom_file(context)
    with _CUSTOM_COLORS_LOCK:
        data = load_json(custom_file, [])
        return data if isinstance(data, list) else []

def save_custom_colors(colors: list[dict[str, Any]], context) -> None:
    """保存用户自定义颜色

    Args:
        colors: 颜色列表
        context: 插件上下文
    """
    custom_file = _custom_file(context)
    with _CUSTOM_COLORS_LOCK:
        ensure_dir(context.data_dir)
        write_json(custom_file, colors)


def mutate_custom_colors(context, callback):
    """Atomically read, mutate and persist one chat/private color library."""
    custom_file = _custom_file(context)
    with _CUSTOM_COLORS_LOCK:
        colors = load_json(custom_file, [])
        if not isinstance(colors, list):
            colors = []
        result = callback(colors)
        ensure_dir(context.data_dir)
        write_json(custom_file, colors)
        return result

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
