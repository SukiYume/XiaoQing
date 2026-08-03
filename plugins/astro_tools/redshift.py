"""
红移计算模块
"""

import math

from core.plugin_base import run_sync
from core.public_errors import public_error_message


def _handle_redshift_sync(args: str, context) -> str:
    """处理红移计算命令"""
    args = args.strip()
    if not args:
        return (
            "请提供红移值\n示例: /astro redshift 0.5\n\n"
            "常用红移范围: 0 到 ~10；计算上限为 1100\n"
            "使用 Planck 2018 宇宙学参数"
        )

    try:
        # 验证输入
        try:
            z = float(args)
        except (ValueError, OverflowError):
            return f"无效的红移值: {args}\n请提供有效的数字"

        if not math.isfinite(z):
            return "红移值必须是有限数字"

        if z < 0:
            return "红移值必须 >= 0\n\n注: z=0 表示当前宇宙，z>0 表示过去的宇宙"

        if z > 1100:
            return (
                f"红移值 {z} 过大\n\n"
                f"注: 宇宙微波背景辐射的红移约为 z≈1100\n"
                f"观测到的最远星系红移约为 z≈13"
            )

        from astropy import units as u
        from astropy.cosmology import Planck18 as cosmo

        # 计算各种距离和时间
        d_L = cosmo.luminosity_distance(z)
        d_A = cosmo.angular_diameter_distance(z)
        d_C = cosmo.comoving_distance(z)
        t_lookback = cosmo.lookback_time(z)
        age_at_z = cosmo.age(z)

        # 低于 1000 Mpc 时保留 Mpc，避免把近邻天体显示成难读的 0.00x Gpc。
        distance_unit = u.Mpc if d_L.to_value(u.Mpc) < 1000 else u.Gpc
        distance_precision = 3 if distance_unit == u.Mpc else 4
        result_lines = [
            "🌌 红移计算 (Planck 2018)",
            "",
            f"**输入红移: z = {z}**",
            "",
            "**距离:**",
            f"光度距离: {d_L.to(distance_unit):.{distance_precision}f}",
            f"角直径距离: {d_A.to(distance_unit):.{distance_precision}f}",
            f"共动距离: {d_C.to(distance_unit):.{distance_precision}f}",
            "",
            "**时间:**",
            f"光行时: {t_lookback.to(u.Gyr):.3f}",
            f"宇宙年龄(当时): {age_at_z.to(u.Gyr):.3f}",
            "",
            "**其他参数:**",
            f"尺度因子: a = 1/(1+z) = {1 / (1 + z):.6f}",
        ]

        # 根据红移给出简短的物理语境，不参与数值计算。
        if z < 0.1:
            context_note = "💡 近邻宇宙 - 可用于局部星系研究"
        elif z < 1:
            context_note = "💡 中等红移 - 星系演化的重要阶段"
        elif z < 3:
            context_note = "💡 高红移 - 星系形成活跃时期"
        else:
            context_note = "💡 极高红移 - 早期宇宙"

        return "\n".join((*result_lines, "", context_note))
    except Exception as exc:
        return public_error_message(
            context, exc, logger=context.logger, component="astro_tools.redshift"
        )


async def handle_redshift(args: str, context) -> str:
    """在线程 bulkhead 中执行 astropy 宇宙学计算。"""

    return await run_sync(_handle_redshift_sync, args, context)
