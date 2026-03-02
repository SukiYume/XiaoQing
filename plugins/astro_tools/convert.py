"""
单位转换模块
"""


async def handle_convert(args: str, context) -> str:
    """处理单位转换命令"""
    args = args.strip()
    if not args:
        return "请提供转换参数\n示例: /astro convert 3 Jy mJy\n\n" \
               "支持的单位:\n" \
               "- 流量密度: Jy, mJy, μJy\n" \
               "- 长度: pc, kpc, Mpc, ly, AU, m, km\n" \
               "- 频率: Hz, kHz, MHz, GHz, THz\n" \
               "- 温度: K, mK\n" \
               "- 能量: eV, keV, MeV, GeV, TeV, J, erg\n" \
               "- 以及所有 astropy 支持的单位 (如 km/s, erg/s)"
    
    try:
        from astropy import units as u
        
        parts = args.split()
        if len(parts) < 3:
            return "格式: /astro convert <数值> <源单位> <目标单位>\n" \
                   "示例: /astro convert 3 Jy mJy"
        
        # 验证数值
        try:
            value = float(parts[0])
        except ValueError:
            return f"无效的数值: {parts[0]}\n请提供有效的数字"
        
        from_unit_str = parts[1]
        to_unit_str = parts[2]
        
        # 扩展的单位映射
        unit_map = {
            # 流量密度
            'jy': u.Jy,
            'mjy': u.mJy,
            'ujy': u.microJy,
            'μjy': u.microJy,
            
            # 长度
            'pc': u.pc,
            'kpc': u.kpc,
            'mpc': u.Mpc,
            'ly': u.lightyear,
            'au': u.au,
            'm': u.m,
            'km': u.km,
            'cm': u.cm,
            
            # 频率
            'hz': u.Hz,
            'khz': u.kHz,
            'mhz': u.MHz,
            'ghz': u.GHz,
            'thz': u.THz,
            
            # 温度
            'k': u.K,
            'mk': u.mK,
            
            # 能量
            'ev': u.eV,
            'kev': u.keV,
            'mev': u.MeV,
            'gev': u.GeV,
            'tev': u.TeV,
            'j': u.J,
            'erg': u.erg,
            
            # 质量
            'msun': u.Msun,
            'kg': u.kg,
            'g': u.g,
        }
        
        from_u = unit_map.get(from_unit_str.lower())
        if from_u is None:
            try:
                from_u = u.Unit(from_unit_str)
            except ValueError:
                return f"不支持的源单位: {from_unit_str}\n\n" \
                       f"支持标准单位符号 (如 m, s, kg) 及 astropy 单位字符串"
        
        to_u = unit_map.get(to_unit_str.lower())
        if to_u is None:
            try:
                to_u = u.Unit(to_unit_str)
            except ValueError:
                return f"不支持的目标单位: {to_unit_str}\n\n" \
                       f"支持标准单位符号 (如 m, s, kg) 及 astropy 单位字符串"
        
        try:
            result = (value * from_u).to(to_u)
            # 智能格式化输出
            if abs(result.value) < 0.001 or abs(result.value) > 1e6:
                result_str = f"{result.value:.6e}"
            else:
                result_str = f"{result.value:.6g}"
            
            return f"📐 单位转换\n" \
                   f"{value} {from_unit_str} = {result_str} {to_unit_str}"
        except u.UnitConversionError:
            return f"无法在 {from_unit_str} 和 {to_unit_str} 之间转换\n" \
                   f"这两个单位的物理量纲不兼容"
    except Exception as exc:
        return f"单位转换失败: {exc}"
