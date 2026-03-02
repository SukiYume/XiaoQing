"""
天文常数查询模块
"""


async def handle_const(args: str, context) -> str:
    """处理天文常数查询命令"""
    args = args.strip().lower()
    if not args:
        return _get_const_list()
    
    try:
        from astropy import constants as const
        from astropy import units as u
        
        # 定义常数映射
        # (中文名, 值)
        const_map = {
            # 基本物理常数
            'c': ('光速', const.c),
            'speed': ('光速', const.c),
            'light': ('光速', const.c),
            'g': ('引力常数', const.G),
            'gravity': ('引力常数', const.G),
            'h': ('普朗克常数', const.h),
            'planck': ('普朗克常数', const.h),
            'k': ('玻尔兹曼常数', const.k_B),
            'boltzmann': ('玻尔兹曼常数', const.k_B),
            'sigma': ('斯特藩-玻尔兹曼常数', const.sigma_sb),
            'stefan': ('斯特藩-玻尔兹曼常数', const.sigma_sb),
            'me': ('电子质量', const.m_e),
            'electron': ('电子质量', const.m_e),
            'mp': ('质子质量', const.m_p),
            'proton': ('质子质量', const.m_p),
            
            # 天文常数 - 太阳
            'm_sun': ('太阳质量', const.M_sun),
            'sun_mass': ('太阳质量', const.M_sun),
            'r_sun': ('太阳半径', const.R_sun),
            'sun_radius': ('太阳半径', const.R_sun),
            'l_sun': ('太阳光度', const.L_sun),
            'sun_luminosity': ('太阳光度', const.L_sun),
            
            # 天文单位
            'au': ('天文单位', const.au),
            'pc': ('秒差距', const.pc),
            'parsec': ('秒差距', const.pc),
            'ly': ('光年', const.pc.to(u.lightyear)), 
            'light_year': ('光年', const.pc.to(u.lightyear)),
            
            # 宇宙学
            'h0': ('哈勃常数 (近似值)', 70.0 * (u.km / u.s / u.Mpc)),
            'hubble': ('哈勃常数 (近似值)', 70.0 * (u.km / u.s / u.Mpc)),
        }
        
        const_info = const_map.get(args)
        if const_info:
            name, value = const_info
            return f"🔢 {name}\n" \
                   f"值: {value}\n" \
                   f"单位: {value.unit}\n" \
                   f"数值: {value.value:.6e}"
        
        return f"未找到常数: {args}\n\n可用常数: {', '.join(sorted(set(k for k in const_map.keys())))}"
    except Exception as exc:
        return f"查询失败: {exc}"


def _get_const_list() -> str:
    """获取常数列表"""
    return "🔢 可用常数列表\n\n" \
           "**基本物理常数:**\n" \
           "c, speed - 光速\n" \
           "g, gravity - 引力常数\n" \
           "h, planck - 普朗克常数\n" \
           "k, boltzmann - 玻尔兹曼常数\n" \
           "sigma, stefan - 斯特藩-玻尔兹曼常数\n" \
           "me - 电子质量\n" \
           "mp - 质子质量\n\n" \
           "**天文常数:**\n" \
           "m_sun - 太阳质量\n" \
           "r_sun - 太阳半径\n" \
           "l_sun - 太阳光度\n" \
           "au - 天文单位\n" \
           "pc - 秒差距\n" \
           "ly - 光年\n" \
           "h0 - 哈勃常数\n\n" \
           "用法: /astro const <常数名>"
