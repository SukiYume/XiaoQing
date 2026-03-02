"""
坐标转换模块
"""


def _is_hms_format(s: str) -> bool:
    """判断是否为 HMS 格式"""
    return ':' in s or 'h' in s.lower() or 'm' in s.lower() or 's' in s.lower()


async def handle_coord(args: str, context) -> str:
    """处理坐标转换命令"""
    args = args.strip()
    if not args:
        return "请提供坐标\n示例: /astro coord 12:34:56 +12:34:56\n或: /astro coord galactic 120 30"
    
    try:
        from astropy.coordinates import SkyCoord
        from astropy import units as u
        
        parts = args.split()
        
        # 处理银道坐标转赤道坐标
        if parts[0].lower() == "galactic":
            if len(parts) < 3:
                return "请提供银经和银纬\n示例: /astro coord galactic 120 30"
            try:
                l = float(parts[1])
                b = float(parts[2])
                coord = SkyCoord(l=l*u.deg, b=b*u.deg, frame='galactic')
                icrs_coord = coord.icrs
                
                ra_hms = icrs_coord.ra.to_string(unit=u.hour, sep=':', pad=True, precision=2)
                dec_dms = icrs_coord.dec.to_string(unit=u.deg, sep=':', pad=True, precision=1, alwayssign=True)
                
                return f"🌐 银道坐标转赤道坐标\n" \
                       f"输入 (银道): l={l}°, b={b}°\n\n" \
                       f"RA (J2000): {ra_hms} ({icrs_coord.ra.deg:.6f}°)\n" \
                       f"Dec (J2000): {dec_dms} ({icrs_coord.dec.deg:.6f}°)"
            except (ValueError, IndexError):
                return "无效的银道坐标格式\n示例: /astro coord galactic 120 30"
        
        # 处理黄道坐标转赤道坐标
        if parts[0].lower() == "ecliptic":
            if len(parts) < 3:
                return "请提供黄经和黄纬\n示例: /astro coord ecliptic 90 23.5"
            try:
                lon = float(parts[1])
                lat = float(parts[2])
                coord = SkyCoord(lon=lon*u.deg, lat=lat*u.deg, frame='geocentrictrueecliptic')
                icrs_coord = coord.icrs
                
                ra_hms = icrs_coord.ra.to_string(unit=u.hour, sep=':', pad=True, precision=2)
                dec_dms = icrs_coord.dec.to_string(unit=u.deg, sep=':', pad=True, precision=1, alwayssign=True)
                
                return f"🌐 黄道坐标转赤道坐标\n" \
                       f"输入 (黄道): λ={lon}°, β={lat}°\n\n" \
                       f"RA (J2000): {ra_hms} ({icrs_coord.ra.deg:.6f}°)\n" \
                       f"Dec (J2000): {dec_dms} ({icrs_coord.dec.deg:.6f}°)"
            except (ValueError, IndexError):
                return "无效的黄道坐标格式\n示例: /astro coord ecliptic 90 23.5"
        
        # 处理赤道坐标（默认）
        if len(parts) < 2:
            return "请提供 RA 和 Dec 两个坐标\n示例: /astro coord 12:34:56 +12:34:56"
        
        ra_str, dec_str = parts[0], parts[1]
        
        ra_is_hms = _is_hms_format(ra_str)
        # dec_str 通常是 DMS 或度数，SkyCoord 能自动处理大部分情况
        # 显式指定单位有助于消除歧义
        
        if ra_is_hms:
            ra_unit = u.hourangle
        else:
            ra_unit = u.deg
            
        dec_unit = u.deg
        
        coord = SkyCoord(ra_str, dec_str, unit=(ra_unit, dec_unit), frame='icrs')
        
        # 转换到银道坐标
        galactic_coord = coord.galactic
        
        # 转换到黄道坐标
        ecliptic_coord = coord.geocentrictrueecliptic
        
        ra_hms = coord.ra.to_string(unit=u.hour, sep=':', pad=True, precision=2)
        dec_dms = coord.dec.to_string(unit=u.deg, sep=':', pad=True, precision=1, alwayssign=True)
        ra_deg = coord.ra.deg
        dec_deg = coord.dec.deg
        
        return f"🌐 坐标转换结果\n" \
               f"输入: {ra_str} {dec_str}\n\n" \
               f"**赤道坐标 (J2000):**\n" \
               f"RA: {ra_hms} ({ra_deg:.6f}°)\n" \
               f"Dec: {dec_dms} ({dec_deg:.6f}°)\n\n" \
               f"**银道坐标:**\n" \
               f"l: {galactic_coord.l.deg:.6f}°\n" \
               f"b: {galactic_coord.b.deg:.6f}°\n\n" \
               f"**黄道坐标:**\n" \
               f"λ: {ecliptic_coord.lon.deg:.6f}°\n" \
               f"β: {ecliptic_coord.lat.deg:.6f}°"
    except ValueError as e:
        return f"坐标格式错误: {e}\n\n支持的格式:\n" \
               "- 赤道: /astro coord 12:34:56 +12:34:56\n" \
               "- 银道: /astro coord galactic 120 30\n" \
               "- 黄道: /astro coord ecliptic 90 23.5"
    except Exception as exc:
        return f"坐标转换失败: {exc}\n\n支持的格式:\n" \
               "- HMS: 12:34:56 或 12h34m56s\n" \
               "- DMS: +12:34:56 或 +12d34m56s\n" \
               "- 度数: 123.456"
