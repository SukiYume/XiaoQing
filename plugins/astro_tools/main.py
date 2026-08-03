# ruff: noqa: I001  # Keep imports separate for the source-based plugin loader.
"""
天文工具插件
提供天文计算、坐标转换、时间转换等功能
"""

import logging

from core.args import parse
from core.plugin_base import Segments, bounded_external_text, segments
from core.public_errors import public_error_response

from . import const
from . import convert
from . import coord
from . import formula
from . import obj
from . import redshift
from . import time as astro_time

logger = logging.getLogger(__name__)

_COMMAND_HANDLERS = {
    "time": astro_time.handle_time,
    "coord": coord.handle_coord,
    "convert": convert.handle_convert,
    "redshift": redshift.handle_redshift,
    "formula": formula.handle_formula,
    "obj": obj.handle_obj,
    "const": const.handle_const,
}

_SUBCOMMAND_HELP = {
    "time": "用法: /astro time [now|jd <值>|mjd <值>|unix <值>|时间值]",
    "coord": (
        "用法: /astro coord <RA> <Dec>\n"
        "或: /astro coord <galactic|ecliptic> <经度> <纬度>"
    ),
    "convert": "用法: /astro convert <数值> <源单位> <目标单位>",
    "redshift": "用法: /astro redshift <红移值>（范围 0 到 1100）",
    "formula": "用法: /astro formula [公式名|list|calc <类型> <质量>]",
    "obj": "用法: /astro obj <天体名称>（也支持 sun/moon/earth 等固定对象）",
    "const": "用法: /astro const [常数名]",
}


async def handle(command: str, args: str, event: dict, context) -> Segments:
    """命令处理入口"""
    try:
        parsed = parse(args)

        if not parsed:
            return segments(_show_help())

        subcommand = parsed.first.lower()

        if len(parsed) == 1 and not parsed.options and subcommand in {"help", "帮助"}:
            return segments(_show_help())

        handler = _COMMAND_HANDLERS.get(subcommand)
        if handler is None:
            safe_subcommand = bounded_external_text(
                subcommand,
                max_chars=32,
                max_bytes=128,
                suffix="…",
            )
            return segments(f"未知命令: {safe_subcommand}\n输入 /astro help 查看帮助")
        subcommand_args = parsed.rest(1)
        if subcommand_args.strip().casefold() in {"help", "帮助"}:
            return segments(_show_help(subcommand))
        return segments(await handler(subcommand_args, context))

    except Exception as exc:
        return public_error_response(context, exc, logger=logger, component="astro_tools.handle")


def _show_help(subcommand: str | None = None) -> str:
    """显示帮助信息"""
    if subcommand in _SUBCOMMAND_HELP:
        return f"🔭 天文工具 · {subcommand}\n\n{_SUBCOMMAND_HELP[subcommand]}\n\n输入 /astro help 查看全部帮助"
    return """
🔭 **天文工具**

**时间转换:**
• /astro time <时间值> - 转换 ISO、JD、MJD 或可识别的数值时间
• /astro time now - 显示当前天文时间
• /astro time jd <儒略日> - 儒略日转日期时间
• /astro time mjd <修正儒略日> - 修正儒略日转日期时间
• /astro time unix <时间戳> - 显式转换 Unix 时间戳

**坐标转换:**
• /astro coord <赤经> <赤纬> - ICRS 赤道坐标格式转换和多坐标系显示
• /astro coord galactic <银经> <银纬> - 银道坐标转 ICRS
• /astro coord ecliptic <黄经> <黄纬> - 黄道坐标转 ICRS

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
