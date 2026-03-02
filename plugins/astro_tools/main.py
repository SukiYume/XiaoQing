"""
天文工具插件
提供天文计算、坐标转换、时间转换等功能
"""
import logging

from core.plugin_base import segments
from core.args import parse

import time as std_time # Rename standard library time just in case, though we might not need it if we use datetime
from . import time as astro_time # Safe import of local time.py
from . import coord
from . import convert
from . import redshift
from . import formula
from . import obj
from . import const


logger = logging.getLogger(__name__)


def init(context=None) -> None:
    """插件初始化"""
    pass


async def handle(command: str, args: str, event: dict, context) -> list:
    """命令处理入口"""
    try:
        parsed = parse(args)
        
        if not parsed:
            return segments(_show_help())
        
        subcommand = parsed.first.lower()
        
        # 命令路由
        if subcommand == "help" or subcommand == "帮助":
            return segments(_show_help())
        
        if subcommand == "time":
            return segments(await astro_time.handle_time(parsed.rest(1), context))
        
        if subcommand == "coord":
            return segments(await coord.handle_coord(parsed.rest(1), context))
        
        if subcommand == "convert":
            return segments(await convert.handle_convert(parsed.rest(1), context))
        
        if subcommand == "redshift":
            return segments(await redshift.handle_redshift(parsed.rest(1), context))
        
        if subcommand == "formula":
            return segments(await formula.handle_formula(parsed.rest(1), context))
        
        if subcommand == "obj":
            return segments(await obj.handle_obj(parsed.rest(1), context))
        
        if subcommand == "const":
            return segments(await const.handle_const(parsed.rest(1), context))
        
        return segments(f"未知命令: {subcommand}\n输入 /astro help 查看帮助")
        
    except Exception as e:
        logger.exception("AstroTools handle error: %s", e)
        return segments(f"处理请求时出错: {str(e)}")


def _show_help() -> str:
    """显示帮助信息"""
    return """
🔭 **天文工具**

**时间转换:**
• /astro time <时间值> - 转换任意时间格式（支持ISO、JD、MJD）
• /astro time now - 显示当前天文时间
• /astro time jd <儒略日> - 儒略日转日期时间
• /astro time mjd <修正儒略日> - 修正儒略日转日期时间

**坐标转换:**
• /astro coord <赤经> <赤纬> - 赤道坐标格式转换和多坐标系显示
• /astro coord galactic <银经> <银纬> - 银道坐标转赤道坐标
• /astro coord ecliptic <黄经> <黄纬> - 黄道坐标转赤道坐标

**单位转换:**
• /astro convert <数值> <源单位> <目标单位>
• 支持单位: Jy, mJy, pc, ly, AU, m, km, Hz, GHz, MHz, K, eV, keV, MeV
• 示例: /astro convert 3 Jy mJy

**红移计算:**
• /astro redshift <红移值> - 计算红移对应的距离和年龄

**天文公式:**
• /astro formula - 查看可用公式列表
• /astro formula <公式名> - 查看公式详情
• /astro formula calc schwarzschild <质量(太阳质量)> - 计算史瓦西半径
• /astro formula calc luminosity <质量(太阳质量)> - 计算主序星光度
• /astro formula calc lifetime <质量(太阳质量)> - 计算主序星寿命

**天体信息:**
• /astro obj <天体名称> - 从SIMBAD查询天体信息
• /astro obj sun - 太阳信息
• /astro obj moon - 月球信息
• /astro obj earth - 地球信息
• /astro obj <行星名> - 太阳系行星信息

**天文常数:**
• /astro const - 查看可用常数列表
• /astro const <常数名> - 查询天文常数值

输入 /astro help 查看此帮助
""".strip()
