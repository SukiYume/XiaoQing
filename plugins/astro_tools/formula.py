"""
公式速查和计算模块
"""

import math
import sys

from core.plugin_base import PluginContextProtocol, run_sync
from core.public_errors import public_error_message

# 公式定义常量
FORMULAS: dict[str, dict[str, str]] = {
    "dm": {
        "name": "色散量 (Dispersion Measure)",
        "formula": "DM = ∫₀ᴸ nₑ dl (pc cm⁻³)",
        "description": "色散量是沿视线方向的自由电子柱密度",
        "delay": "Δt = 4.15 × DM × (ν₁⁻² - ν₂⁻²) ms (ν in GHz)",
    },
    "redshift": {
        "name": "红移 (Redshift)",
        "formula": "z = (λ_obs - λ_emit) / λ_emit = Δλ/λ",
        "description": "宇宙学红移，表示宇宙膨胀导致的波长拉伸",
        "notes": "z > 0 表示远离，z < 0 表示靠近",
    },
    "luminosity": {
        "name": "光度距离 (Luminosity Distance)",
        "formula": "d_L = (1+z) × d_C",
        "description": "光度距离与共动距离的关系",
        "notes": "d_C 是共动距离，d_A = d_C/(1+z) 是角直径距离",
    },
    "flux": {
        "name": "流量密度 (Flux Density)",
        "formula": "S_ν = ∫ I_ν dΩ",
        "description": "单位频率的流量，单位 Jy (1 Jy = 10⁻²⁶ W m⁻² Hz⁻¹)",
        "notes": "常用于射电天文学",
    },
    "blackbody": {
        "name": "黑体辐射 (Blackbody Radiation)",
        "formula": "B_ν(T) = (2hν³/c²) × 1/(exp(hν/kT) - 1)",
        "description": "普朗克黑体辐射公式",
        "notes": "h: 普朗克常数, k: 玻尔兹曼常数, c: 光速",
    },
    "parallax": {
        "name": "视差距离 (Parallax Distance)",
        "formula": "d = 1/π (pc)",
        "description": "通过视差测量距离，π 是视差角（角秒）",
        "notes": "1 pc = 3.26 光年 = 206265 AU",
    },
    "schwarzschild": {
        "name": "史瓦西半径 (Schwarzschild Radius)",
        "formula": "R_s = 2GM/c²",
        "description": "黑洞的事件视界半径",
        "notes": "对于1太阳质量：R_s ≈ 3 km\n使用 calc schwarzschild <质量> 进行计算",
    },
    "stellar_luminosity": {
        "name": "主序星质光关系 (Mass-Luminosity)",
        "formula": "L/L_☉ ≈ C × (M/M_☉)^α，C 和 α 按质量分段",
        "description": "主序星的光度与质量的经验关系",
        "notes": "对于不同质量范围α有所不同\n使用 calc luminosity <质量> 进行计算",
    },
    "stellar_lifetime": {
        "name": "主序星寿命 (Main Sequence Lifetime)",
        "formula": "t_MS ≈ 10¹⁰ × (M/M_☉)^(-2.5) 年",
        "description": "主序星的寿命估算",
        "notes": "基于核燃料消耗速率\n使用 calc lifetime <质量> 进行计算",
    },
}

_CALCULATION_USAGE = {
    "schwarzschild": "请提供质量（以太阳质量为单位）\n示例: /astro formula calc schwarzschild 10",
    "luminosity": "请提供质量（以太阳质量为单位）\n示例: /astro formula calc luminosity 2",
    "lifetime": "请提供质量（以太阳质量为单位）\n示例: /astro formula calc lifetime 2",
}


def _parse_positive_mass(value: str) -> float | None:
    try:
        mass = float(value)
    except (ValueError, OverflowError):
        return None
    return mass if math.isfinite(mass) and mass > 0 else None


def _handle_formula_sync(args: str, context: PluginContextProtocol) -> str:
    """处理公式速查和计算命令"""
    args = args.strip().casefold()
    if not args or args in {"list", "help", "帮助"}:
        return _get_formula_list()

    parts      = args.split(None, 1)
    subcommand = parts[0]

    # 处理计算子命令
    if subcommand == "calc":
        if len(parts) < 2:
            return "请提供计算类型和参数\n示例: /astro formula calc schwarzschild 10"
        return _handle_calculation(parts[1], context)

    # 查询公式定义
    formula = FORMULAS.get(args)
    if formula is not None:
        result_lines = [
            f"📜 {formula['name']}",
            "",
            f"公式: {formula['formula']}",
            f"说明: {formula['description']}",
        ]
        if "delay" in formula:
            result_lines.append(f"延迟: {formula['delay']}")
        if "notes" in formula:
            result_lines.append(f"备注: {formula['notes']}")
        return "\n".join(result_lines)

    return f"未找到公式: {args}\n\n可用公式: {', '.join(FORMULAS)}\n\n输入 /astro formula 查看列表"


async def handle_formula(args: str, context: PluginContextProtocol) -> str:
    """在线程 bulkhead 中执行公式和 astropy 计算。"""

    return await run_sync(_handle_formula_sync, args, context)


def _handle_calculation(args: str, context: PluginContextProtocol) -> str:
    """处理公式计算"""
    parts = args.strip().split(None, 1)
    if not parts:
        return "请指定计算类型\n示例: /astro formula calc schwarzschild 10"

    calc_type = parts[0].casefold()
    if calc_type not in _CALCULATION_USAGE:
        return (
            f"未知的计算类型: {calc_type}\n\n"
            "可用计算:\n"
            "- schwarzschild <质量> - 史瓦西半径\n"
            "- luminosity <质量> - 主序星光度\n"
            "- lifetime <质量> - 主序星寿命"
        )
    if len(parts) < 2:
        return _CALCULATION_USAGE[calc_type]

    mass_solar = _parse_positive_mass(parts[1])
    if mass_solar is None:
        return "质量必须是有限的正数"

    try:
        if calc_type == "schwarzschild":
            from astropy import constants as const
            from astropy import units as u

            radius_per_solar_mass = (2 * const.G * const.M_sun / const.c**2).to_value(u.km)
            if mass_solar > sys.float_info.max / radius_per_solar_mass:
                return "质量超出可计算范围"
            radius_km = mass_solar * radius_per_solar_mass
            if not math.isfinite(radius_km):
                return "质量超出可计算范围"
            return (
                "⚫ 史瓦西半径计算\n\n"
                f"质量: {mass_solar} M☉\n"
                f"史瓦西半径: {radius_km:.3f} km\n"
                "参考: 太阳的史瓦西半径约为 3 km"
            )

        if calc_type == "luminosity":
            # UNLV IAL 23 的经验关系；分段系数取约数，边界保留小幅跳跃。
            # 来源：https://www.physics.unlv.edu/~jeffery/astro/ial/ial_023.html
            if mass_solar < 0.43:
                alpha       = 2.3
                coefficient = 0.23
            elif mass_solar < 2:
                alpha       = 4.0
                coefficient = 1.0
            elif mass_solar < 20:
                alpha       = 3.5
                coefficient = 1.5
            else:
                alpha       = 1.0
                coefficient = 2500.0

            luminosity = coefficient * mass_solar**alpha
            if not math.isfinite(luminosity):
                return "质量超出可计算范围"
            return (
                "⭐ 主序星光度估算\n\n"
                f"质量: {mass_solar} M☉\n"
                f"估算光度: {luminosity:.3e} L☉\n"
                f"使用指数: α = {alpha}\n"
                f"归一化系数: C = {coefficient:.6g}\n"
                "备注: 分段经验近似在边界有小幅跳跃，结果还依赖恒星年龄和金属丰度"
            )

        lifetime_years = 1e10 * (mass_solar**-2.5)
        if not math.isfinite(lifetime_years) or lifetime_years <= 0:
            return "质量超出可计算范围"
        if lifetime_years > 1e9:
            time_str = f"{lifetime_years / 1e9:.3f} Gyr (十亿年)"
        elif lifetime_years > 1e6:
            time_str = f"{lifetime_years / 1e6:.3f} Myr (百万年)"
        else:
            time_str = f"{lifetime_years:.3e} 年"

        return (
            "⏳ 主序星寿命估算\n\n"
            f"质量: {mass_solar} M☉\n"
            f"估算主序寿命: {time_str}\n"
            "备注: 这是基于 t_MS ≈ 10¹⁰ × (M/M☉)^(-2.5) 年的估算\n"
            "参考: 太阳的主序寿命约为 10 Gyr"
        )

    except OverflowError:
        return "质量超出可计算范围"
    except Exception as exc:
        return public_error_message(
            context, exc, logger=context.logger, component="astro_tools.formula"
        )


def _get_formula_list() -> str:
    """获取公式列表"""
    keys_list = "\n".join(
        f"{key} - {definition['name'].split('(')[0].strip()}"
        for key, definition in FORMULAS.items()
    )
    return (
        "📜 天文公式工具\n\n"
        "**可用公式:**\n"
        f"{keys_list}\n\n"
        "**进行计算:**\n"
        "/astro formula calc schwarzschild <质量(M☉)> - 计算史瓦西半径\n"
        "/astro formula calc luminosity <质量(M☉)> - 估算主序星光度\n"
        "/astro formula calc lifetime <质量(M☉)> - 估算主序星寿命\n\n"
        "用法: /astro formula <公式名> 或 /astro formula calc <计算类型> <参数>"
    )
