"""坐标转换：赤道坐标格式互转并换算银道/黄道，或把银道/黄道坐标转为 ICRS。"""

import math

from core.plugin_base import PluginContextProtocol
from core.public_errors import public_error_message

# 非赤道输入：标题、astropy 坐标系、(参数名, 符号, 中文名) 的经度与纬度、示例
_FRAME_INPUTS = {
    "galactic": (
        "银道",
        "galactic",
        ("l", "l", "银经"),
        ("b", "b", "银纬"),
        "/astro coord galactic 120 30",
    ),
    "ecliptic": (
        "黄道",
        "geocentrictrueecliptic",
        ("lon", "λ", "黄经"),
        ("lat", "β", "黄纬"),
        "/astro coord ecliptic 90 23.5",
    ),
}


def _finite_degrees(*values: str) -> tuple[float, ...]:
    """解析一组角度，并在构造坐标前拒绝 NaN/Inf。"""

    parsed = tuple(float(value) for value in values)
    if not all(math.isfinite(value) for value in parsed):
        raise ValueError("coordinates must be finite")
    return parsed


def handle_coord(args: str, context: PluginContextProtocol) -> str:
    """处理坐标转换命令"""
    args = args.strip()
    if not args:
        return "请提供坐标\n示例: /astro coord 12:34:56 +12:34:56\n或: /astro coord galactic 120 30"

    try:
        from astropy import units as u
        from astropy.coordinates import SkyCoord

        parts = args.split()

        # 银道/黄道坐标转赤道坐标
        frame = _FRAME_INPUTS.get(parts[0].lower())
        if frame is not None:
            (
                title,
                frame_name,
                (lon_key, lon_symbol, lon_name),
                (lat_key, lat_symbol, lat_name),
                example,
            ) = frame
            if len(parts) != 3:
                return f"请提供{lon_name}和{lat_name}\n示例: {example}"
            try:
                lon, lat = _finite_degrees(parts[1], parts[2])
            except ValueError:
                return f"无效的{title}坐标格式\n示例: {example}"
            icrs_coord = SkyCoord(
                frame=frame_name, **{lon_key: lon * u.deg, lat_key: lat * u.deg}
            ).icrs
            ra_hms = icrs_coord.ra.to_string(unit=u.hour, sep=":", pad=True, precision=2)
            dec_dms = icrs_coord.dec.to_string(
                unit=u.deg, sep=":", pad=True, precision=1, alwayssign=True
            )
            return (
                f"🌐 {title}坐标转赤道坐标\n"
                f"输入 ({title}): {lon_symbol}={lon}°, {lat_symbol}={lat}°\n\n"
                f"RA (ICRS): {ra_hms} ({icrs_coord.ra.deg:.6f}°)\n"
                f"Dec (ICRS): {dec_dms} ({icrs_coord.dec.deg:.6f}°)"
            )

        # 处理赤道坐标（默认）
        if len(parts) != 2:
            return "请提供 RA 和 Dec 两个坐标\n示例: /astro coord 12:34:56 +12:34:56"

        ra_str, dec_str = parts[0], parts[1]

        # RA 含时分秒标记时按时角解析，否则按度；Dec 始终按度（含 DMS 写法）
        ra_is_hms = any(marker in ra_str.lower() for marker in (":", "h", "m", "s"))
        coord     = SkyCoord(
            ra_str, dec_str, unit=(u.hourangle if ra_is_hms else u.deg, u.deg), frame="icrs"
        )
        if not all(math.isfinite(float(value)) for value in (coord.ra.deg, coord.dec.deg)):
            raise ValueError("coordinates must be finite")

        galactic_coord = coord.galactic
        ecliptic_coord = coord.geocentrictrueecliptic
        ra_hms = coord.ra.to_string(unit=u.hour, sep=":", pad=True, precision=2)
        dec_dms = coord.dec.to_string(unit=u.deg, sep=":", pad=True, precision=1, alwayssign=True)

        return (
            f"🌐 坐标转换结果\n"
            f"输入: {ra_str} {dec_str}\n\n"
            f"赤道坐标 (ICRS):\n"
            f"RA: {ra_hms} ({coord.ra.deg:.6f}°)\n"
            f"Dec: {dec_dms} ({coord.dec.deg:.6f}°)\n\n"
            f"银道坐标:\n"
            f"l: {galactic_coord.l.deg:.6f}°\n"
            f"b: {galactic_coord.b.deg:.6f}°\n\n"
            f"黄道坐标:\n"
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
