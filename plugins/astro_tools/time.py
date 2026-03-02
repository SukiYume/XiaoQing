"""
时间转换模块
"""

import re


async def handle_time(args: str, context) -> str:
    """处理时间转换命令"""
    args = args.strip()
    
    # 如果没有参数，显示当前时间
    if not args:
        return _get_current_time()
    
    try:
        from astropy.time import Time
        
        # 解析子命令
        parts = args.split(None, 1)
        subcommand = parts[0].lower()
        
        # 处理 now 子命令
        if subcommand == "now":
            return _get_current_time()
        
        # 处理 jd 子命令
        if subcommand == "jd":
            if len(parts) < 2:
                return "请提供儒略日数值\n示例: /astro time jd 2460419.5"
            try:
                jd_val = float(parts[1])
                t = Time(jd_val, format='jd')
                return f"🕐 JD {jd_val} 转换结果\n" \
                       f"UTC: {t.iso}\n" \
                       f"JD: {t.jd:.6f}\n" \
                       f"MJD: {t.mjd:.6f}\n" \
                       f"Unix: {t.unix:.2f}\n" \
                       f"格林威治恒星时: {t.sidereal_time('apparent', longitude='greenwich').to_string(sep=':', precision=0)}"
            except ValueError:
                return "无效的儒略日数值"
        
        # 处理 mjd 子命令
        if subcommand == "mjd":
            if len(parts) < 2:
                return "请提供修正儒略日数值\n示例: /astro time mjd 60419.5"
            try:
                mjd_val = float(parts[1])
                t = Time(mjd_val, format='mjd')
                return f"🕐 MJD {mjd_val} 转换结果\n" \
                       f"UTC: {t.iso}\n" \
                       f"JD: {t.jd:.6f}\n" \
                       f"MJD: {t.mjd:.6f}\n" \
                       f"Unix: {t.unix:.2f}\n" \
                       f"格林威治恒星时: {t.sidereal_time('apparent', longitude='greenwich').to_string(sep=':', precision=0)}"
            except ValueError:
                return "无效的修正儒略日数值"
        
        # 处理一般的时间格式（单个数字假定为MJD）
        mjd_match = re.match(r'^(\d+\.?\d*)$', args)
        if mjd_match:
            mjd = float(mjd_match.group(1))
            # 判断是JD还是MJD（JD通常 > 2400000）
            if mjd > 2400000:
                t = Time(mjd, format='jd')
                time_type = "JD"
            else:
                t = Time(mjd, format='mjd')
                time_type = "MJD"
            return f"🕐 {time_type} {mjd} 转换结果\n" \
                   f"UTC: {t.iso}\n" \
                   f"JD: {t.jd:.6f}\n" \
                   f"MJD: {t.mjd:.6f}\n" \
                   f"Unix: {t.unix:.2f}\n" \
                   f"格林威治恒星时: {t.sidereal_time('apparent', longitude='greenwich').to_string(sep=':', precision=0)}"
        
        # 处理其他时间格式（ISO等）
        t = Time(args)
        return f"🕐 {args} 转换结果\n" \
               f"UTC: {t.iso}\n" \
               f"JD: {t.jd:.6f}\n" \
               f"MJD: {t.mjd:.6f}\n" \
               f"Unix: {t.unix:.2f}\n" \
               f"格林威治恒星时: {t.sidereal_time('apparent', longitude='greenwich').to_string(sep=':', precision=0)}"
    except ValueError as e:
        return f"时间格式错误: {e}\n\n支持的格式:\n- ISO: 2026-01-30 或 2026-01-30T12:00:00\n- JD: 2460419.5 (使用 'jd' 子命令)\n- MJD: 60419.5 (使用 'mjd' 子命令)\n- Unix时间戳: 1706616000"
    except Exception as exc:
        return f"时间转换失败: {exc}"


def _get_current_time() -> str:
    """获取当前天文时间"""
    from astropy.time import Time
    t = Time.now()
    return f"🕐 当前天文时间\n" \
           f"UTC: {t.iso}\n" \
           f"JD: {t.jd:.6f}\n" \
           f"MJD: {t.mjd:.6f}\n" \
           f"Unix: {t.unix:.2f}\n" \
           f"格林威治恒星时: {t.sidereal_time('apparent', longitude='greenwich').to_string(sep=':', precision=0)}"
