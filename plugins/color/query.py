"""
颜色查询模块
提供颜色的各种查询功能
"""
from typing import Any, Optional

MAX_SEARCH_RESULTS = 20

def find_by_name(colors: list[dict], name: str) -> Optional[dict]:
    """按名称查找颜色
    
    Args:
        colors: 颜色列表
        name: 颜色名称
        
    Returns:
        找到的颜色数据，未找到返回 None
    """
    for color in colors:
        if color.get('name') == name:
            return color
    return None

def find_by_rgb(colors: list[dict], rgb: list[int]) -> Optional[dict]:
    """按 RGB 查找颜色
    
    Args:
        colors: 颜色列表
        rgb: RGB 值列表 [R, G, B]
        
    Returns:
        找到的颜色数据，未找到返回 None
    """
    for color in colors:
        if color.get('RGB') == rgb:
            return color
    return None

def find_by_hex(colors: list[dict], hex_value: str) -> Optional[dict]:
    """按 HEX 查找颜色
    
    Args:
        colors: 颜色列表
        hex_value: HEX 颜色值
        
    Returns:
        找到的颜色数据，未找到返回 None
    """
    if not hex_value.startswith('#'):
        hex_value = '#' + hex_value
    for color in colors:
        if color.get('hex', '').lower() == hex_value.lower():
            return color
    return None

def find_by_cmyk(colors: list[dict], cmyk: list[int]) -> Optional[dict]:
    """按 CMYK 查找颜色
    
    Args:
        colors: 颜色列表
        cmyk: CMYK 值列表 [C, M, Y, K]
        
    Returns:
        找到的颜色数据，未找到返回 None
    """
    for color in colors:
        if color.get('CMYK') == cmyk:
            return color
    return None

def find_by_keyword(colors: list[dict], keyword: str) -> list[dict]:
    """按关键词搜索颜色
    
    Args:
        colors: 颜色列表
        keyword: 搜索关键词
        
    Returns:
        匹配的颜色列表
    """
    return [c for c in colors if keyword in c.get('name', '')]
