"""
颜色转换模块
提供 RGB、CMYK、HEX 之间的转换和验证功能
"""
from typing import Optional

# 常量配置
RGB_MIN = 0
RGB_MAX = 255
CMYK_MIN = 0
CMYK_MAX = 100

def validate_rgb(rgb: list[int]) -> tuple[bool, Optional[str]]:
    """验证 RGB 值的有效性
    
    Args:
        rgb: RGB 值列表 [R, G, B]
        
    Returns:
        (是否有效, 错误信息)
    """
    if len(rgb) != 3:
        return False, f"RGB 需要3个值，实际提供了 {len(rgb)} 个"
    
    for i, val in enumerate(rgb):
        if not isinstance(val, int):
            return False, f"RGB 第 {i+1} 个值必须是整数"
        if not (RGB_MIN <= val <= RGB_MAX):
            return False, f"RGB 值必须在 {RGB_MIN}-{RGB_MAX} 范围内，第 {i+1} 个值为 {val}"
    
    return True, None

def validate_cmyk(cmyk: list[int]) -> tuple[bool, Optional[str]]:
    """验证 CMYK 值的有效性
    
    Args:
        cmyk: CMYK 值列表 [C, M, Y, K]
        
    Returns:
        (是否有效, 错误信息)
    """
    if len(cmyk) != 4:
        return False, f"CMYK 需要4个值，实际提供了 {len(cmyk)} 个"
    
    for i, val in enumerate(cmyk):
        if not isinstance(val, int):
            return False, f"CMYK 第 {i+1} 个值必须是整数"
        if not (CMYK_MIN <= val <= CMYK_MAX):
            return False, f"CMYK 值必须在 {CMYK_MIN}-{CMYK_MAX} 范围内，第 {i+1} 个值为 {val}"
    
    return True, None

def rgb_to_cmyk(rgb: list[int]) -> list[int]:
    """RGB 转 CMYK
    
    Args:
        rgb: RGB 值列表 [R, G, B]
        
    Returns:
        CMYK 值列表 [C, M, Y, K]
    """
    r, g, b = [int(x) for x in rgb]
    RGB_SCALE = RGB_MAX
    CMYK_SCALE = CMYK_MAX
    
    if (r, g, b) == (0, 0, 0):
        return [0, 0, 0, CMYK_SCALE]
    
    c = 1 - r / RGB_SCALE
    m = 1 - g / RGB_SCALE
    y = 1 - b / RGB_SCALE
    min_cmy = min(c, m, y)
    
    c = (c - min_cmy) / (1 - min_cmy)
    m = (m - min_cmy) / (1 - min_cmy)
    y = (y - min_cmy) / (1 - min_cmy)
    k = min_cmy
    
    return [int(c * CMYK_SCALE), int(m * CMYK_SCALE), int(y * CMYK_SCALE), int(k * CMYK_SCALE)]

def hex_to_rgb(hex_value: str) -> list[int]:
    """HEX 转 RGB
    
    Args:
        hex_value: HEX 颜色值，支持 #RRGGBB 或 RRGGBB 格式
        
    Returns:
        RGB 值列表 [R, G, B]
        
    Raises:
        ValueError: 如果 HEX 格式无效
    """
    value = hex_value.lstrip('#')
    if len(value) not in [3, 6]:
        raise ValueError(f"HEX 颜色值长度必须是3或6，当前为 {len(value)}")
    
    if len(value) == 3:
        # 支持简写格式 #RGB -> #RRGGBB
        value = ''.join([c*2 for c in value])
    
    try:
        return [int(value[i:i+2], 16) for i in range(0, 6, 2)]
    except ValueError as e:
        raise ValueError(f"无效的 HEX 颜色值: {hex_value}") from e

def rgb_to_hex(rgb: list[int]) -> str:
    """RGB 转 HEX
    
    Args:
        rgb: RGB 值列表 [R, G, B]
        
    Returns:
        HEX 颜色值字符串，格式为 #RRGGBB
    """
    return '#%02x%02x%02x' % tuple(int(x) for x in rgb)
