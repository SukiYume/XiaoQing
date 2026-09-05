"""
坐标转换模块
"""

import math

from core.plugin_base import PluginContextProtocol, run_sync
from core.public_errors import public_error_message


def _finite_degrees(*values: str) -> tuple[float, ...]:
    """解析一组角度，并在构造坐标前拒绝 NaN/Inf。"""

    parsed = tuple(float(value) for value in values)
    if not all(math.isfinite(value) for value in parsed):
        raise ValueError("coordinates must be finite")
    return parsed


def _handle_coord_sync(args: str, context: PluginContextProtocol) -> str:
    """处理坐标转换命令"""
    args = args.strip()
    if not args:
        return "请提供坐标\n示例: /astro coord 12:34:56 +12:34:56\n或: /astro coord galactic 120 30"

    try:
        from astropy import units as u
        from astropy.coordinates import SkyCoord

        parts = args.split()

        # 处理银道坐标转赤道坐标
        if parts[0].lower() == "galactic":
            if len(parts) != 3:
                return "请提供银经和银纬\n示例: /astro coord galactic 120 30"
            try:
                galactic_lon, b = _finite_degrees(parts[1], parts[2])
                coord = SkyCoord(l=galactic_lon * u.deg, b=b * u.deg, frame="galactic")
                icrs_coord = coord.icrs

                ra_hms = icrs_coord.ra.to_string(unit=u.hour, sep=":", pad=True, precision=2)
                dec_dms = icrs_coord.dec.to_string(
                    unit=u.deg, sep=":", pad=True, precision=1, alwayssign=True
                )

                return (
                    f"🌐 银道坐标转赤道坐标\n"
                    f"输入 (银道): l={galactic_lon}°, b={b}°\n\n"
                    f"RA (ICRS): {ra_hms} ({icrs_coord.ra.deg:.6f}°)\n"
                    f"Dec (ICRS): {dec_dms} ({icrs_coord.dec.deg:.6f}°)"
                )
            except ValueError:
                return "无效的银道坐标格式\n示例: /astro coord galactic 120 30"

        # 处理黄道坐标转赤道坐标
        if parts[0].lower() == "ecliptic":
            if len(parts) != 3:
                return "请提供黄经和黄纬\n示例: /astro coord ecliptic 90 23.5"
            try:
                lon, lat = _finite_degrees(parts[1], parts[2])
                coord = SkyCoord(lon=lon * u.deg, lat=lat * u.deg, frame="geocentrictrueecliptic")
                icrs_coord = coord.icrs

                ra_hms = icrs_coord.ra.to_string(unit=u.hour, sep=":", pad=True, precision=2)
                dec_dms = icrs_coord.dec.to_string(
                    unit=u.deg, sep=":", pad=True, precision=1, alwayssign=True
                )

                return (
                    f"🌐 黄道坐标转赤道坐标\n"
                    f"输入 (黄道): λ={lon}°, β={lat}°\n\n"
                    f"RA (ICRS): {ra_hms} ({icrs_coord.ra.deg:.6f}°)\n"
                    f"Dec (ICRS): {dec_dms} ({icrs_coord.dec.deg:.6f}°)"
                )
            except ValueError:
                return "无效的黄道坐标格式\n示例: /astro coord ecliptic 90 23.5"

        # 处理赤道坐标（默认）
        if len(parts) != 2:
            return "请提供 RA 和 Dec 两个坐标\n示例: /astro coord 12:34:56 +12:34:56"

        ra_str, dec_str = parts[0], parts[1]

        normalized_ra = ra_str.lower()
        ra_is_hms     = any(marker in normalized_ra for marker in (":", "h", "m", "s"))
        # dec_str 通常是 DMS 或度数，SkyCoord 能自动处理大部分情况
        # 显式指定单位有助于消除歧义

        if ra_is_hms:
            ra_unit = u.hourangle
        else:
            ra_unit = u.deg

        dec_unit = u.deg

        coord = SkyCoord(ra_str, dec_str, unit=(ra_unit, dec_unit), frame="icrs")
        if not all(math.isfinite(float(value)) for value in (coord.ra.deg, coord.dec.deg)):
            raise ValueError("coordinates must be finite")

        # 转换到银道坐标
        galactic_coord = coord.galactic

        # 转换到黄道坐标
        ecliptic_coord = coord.geocentrictrueecliptic

        ra_hms = coord.ra.to_string(unit=u.hour, sep=":", pad=True, precision=2)
        dec_dms = coord.dec.to_string(unit=u.deg, sep=":", pad=True, precision=1, alwayssign=True)
        ra_deg  = coord.ra.deg
        dec_deg = coord.dec.deg

        return (
            f"🌐 坐标转换结果\n"
            f"输入: {ra_str} {dec_str}\n\n"
            f"**赤道坐标 (ICRS):**\n"
            f"RA: {ra_hms} ({ra_deg:.6f}°)\n"
            f"Dec: {dec_dms} ({dec_deg:.6f}°)\n\n"
            f"**银道坐标:**\n"
            f"l: {galactic_coord.l.deg:.6f}°\n"
            f"b: {galactic_coord.b.deg:.6f}°\n\n"
            f"**黄道坐标:**\n"
            f"λ: {ecliptic_coord.lon.deg:.6f}°\n"
            f"β: {ecliptic_coord.lat.deg:.6f}°"
        )
    except ValueError:
        return (
            "坐标格式错误，请检查输入。\n\n支持的格式:\n"
            "- 赤道: /astro coord 12:34:56 +12:34:56\n"
            "- 银道: /astro coord galactic 120 30\n"
            "- 黄道: /astro coord ecliptic 90 23.5"
        )
    except Exception as exc:
        return public_error_message(
            context, exc, logger=context.logger, component="astro_tools.coord"
        )


async def handle_coord(args: str, context: PluginContextProtocol) -> str:
    """在线程 bulkhead 中执行 astropy 坐标计算。"""

    return await run_sync(_handle_coord_sync, args, context)
