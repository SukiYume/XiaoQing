"""
天文常数查询模块
"""

from core.plugin_base import PluginContextProtocol, run_sync
from core.public_errors import public_error_message

_CONST_HELP = (
    "🔢 可用常数列表\n\n"
    "**基本物理常数:**\n"
    "c, speed - 光速\n"
    "g, gravity - 引力常数\n"
    "h, planck - 普朗克常数\n"
    "k, boltzmann - 玻尔兹曼常数\n"
    "sigma, stefan - 斯特藩-玻尔兹曼常数\n"
    "me - 电子质量\n"
    "mp - 质子质量\n\n"
    "**天文常数:**\n"
    "m_sun - 太阳质量\n"
    "r_sun - 太阳半径\n"
    "l_sun - 太阳光度\n"
    "au - 天文单位\n"
    "pc - 秒差距\n"
    "ly - 光年\n"
    "h0 - 哈勃常数\n\n"
    "用法: /astro const <常数名>"
)

_CONST_ALIASES = {
    "speed": "c",
    "light": "c",
    "gravity": "g",
    "planck": "h",
    "boltzmann": "k",
    "stefan": "sigma",
    "electron": "me",
    "proton": "mp",
    "sun_mass": "m_sun",
    "sun_radius": "r_sun",
    "sun_luminosity": "l_sun",
    "parsec": "pc",
    "light_year": "ly",
    "hubble": "h0",
}


def _handle_const_sync(args: str, context: PluginContextProtocol) -> str:
    """处理天文常数查询命令"""
    args = args.strip().casefold()
    if not args:
        return _CONST_HELP

    try:
        from astropy import constants as const
        from astropy import units as u

        # 这里只保存规范名；别名先归一化，避免同一个常数对象重复登记。
        const_map = {
            # 基本物理常数
            "c": ("光速", const.c),
            "g": ("引力常数", const.G),
            "h": ("普朗克常数", const.h),
            "k": ("玻尔兹曼常数", const.k_B),
            "sigma": ("斯特藩-玻尔兹曼常数", const.sigma_sb),
            "me": ("电子质量", const.m_e),
            "mp": ("质子质量", const.m_p),
            # 天文常数 - 太阳
            "m_sun": ("太阳质量", const.M_sun),
            "r_sun": ("太阳半径", const.R_sun),
            "l_sun": ("太阳光度", const.L_sun),
            # 天文单位
            "au": ("天文单位", const.au),
            "pc": ("秒差距", const.pc),
            "ly": ("光年", 1 * u.lightyear),
            # 宇宙学
            "h0": ("哈勃常数 (近似值)", 70.0 * (u.km / u.s / u.Mpc)),
        }

        const_info = const_map.get(_CONST_ALIASES.get(args, args))
        if const_info is not None:
            name, value = const_info
            return f"🔢 {name}\n值: {value}\n单位: {value.unit}\n数值: {value.value:.6e}"

        available_names = sorted((*const_map, *_CONST_ALIASES))
        return f"未找到常数: {args}\n\n可用常数: {', '.join(available_names)}"
    except Exception as exc:
        return public_error_message(
            context, exc, logger=context.logger, component="astro_tools.const"
        )


async def handle_const(args: str, context: PluginContextProtocol) -> str:
    """在线程 bulkhead 中执行 astropy 常数查询。"""

    return await run_sync(_handle_const_sync, args, context)
