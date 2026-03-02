"""
天文对象查询模块
"""


async def handle_obj(args: str, context) -> str:
    """处理天文对象查询命令"""
    args = args.strip()
    if not args:
        return "请提供天体名称\n示例: /astro obj Crab Pulsar\n或: /astro obj sun"
    
    obj_name = args.lower()
    
    # from .obj import SOLAR_SYSTEM # if circular import was an issue, but here it is same file
    
    if obj_name in SOLAR_SYSTEM:
        return SOLAR_SYSTEM[obj_name]()
    
    # 从SIMBAD查询其他天体
    try:
        from astroquery.simbad import Simbad
        from astropy.coordinates import SkyCoord
        from astropy import units as u
        
        Simbad.reset_votable_fields()
        Simbad.add_votable_fields('otype', 'flux(V)', 'sp')
        
        result = Simbad.query_object(args)
        
        if result is None or len(result) == 0:
            return f"未找到天体: {args}\n\n提示: 可以尝试使用英文名称，如 'Crab Nebula', 'Betelgeuse' 等"
        
        row = result[0]
        
        ra_str = str(row['RA'])
        dec_str = str(row['DEC'])
        
        coord = SkyCoord(ra=ra_str, dec=dec_str, unit=(u.hourangle, u.deg), frame='icrs')
        ra_hms = coord.ra.to_string(unit=u.hour, sep=':', pad=True, precision=2)
        dec_dms = coord.dec.to_string(unit=u.deg, sep=':', pad=True, precision=1, alwayssign=True)
        
        result_text = f"🌟 {args}\n\n"
        result_text += f"**坐标 (J2000):**\n"
        result_text += f"RA: {ra_hms} ({coord.ra.deg:.6f}°)\n"
        result_text += f"Dec: {dec_dms} ({coord.dec.deg:.6f}°)\n\n"
        
        # 添加天体类型
        if 'OTYPE' in row.colnames and row['OTYPE']:
            result_text += f"类型: {row['OTYPE']}\n"
        
        # 添加V波段星等
        if 'FLUX_V' in row.colnames and row['FLUX_V']:
            result_text += f"V星等: {row['FLUX_V']:.2f}\n"
        
        # 添加光谱型
        if 'SP_TYPE' in row.colnames and row['SP_TYPE']:
            result_text += f"光谱型: {row['SP_TYPE']}"
        
        return result_text
    except Exception as exc:
        return f"查询失败: {exc}\n\n建议: 使用标准天体名称，如 'M31', 'NGC 1952', 'Sirius' 等"


def _get_sun_info() -> str:
    """获取太阳信息"""
    from astropy import constants as const
    from astropy import units as u
    
    mass = const.M_sun.to(u.kg)
    radius = const.R_sun.to(u.km)
    luminosity = const.L_sun.to(u.W)
    
    return f"☀️ 太阳 (Sun)\n\n" \
           f"**基本参数:**\n" \
           f"质量: {mass.value:.3e} kg (1 M☉)\n" \
           f"半径: {radius.value:,.0f} km (109 R⊕)\n" \
           f"光度: {luminosity.value:.3e} W (1 L☉)\n" \
           f"有效温度: 5778 K\n" \
           f"光谱型: G2V\n" \
           f"年龄: ~4.6 Gyr\n\n" \
           f"**轨道参数:**\n" \
           f"到地球平均距离: 1 AU = 1.496×10⁸ km\n" \
           f"银河系轨道周期: ~230 Myr\n\n" \
           f"**组成:**\n" \
           f"氢: ~73%\n" \
           f"氦: ~25%\n" \
           f"其他元素: ~2%"


def _get_moon_info() -> str:
    """获取月球信息"""
    return f"🌙 月球 (Moon)\n\n" \
           f"**基本参数:**\n" \
           f"质量: 7.342×10²² kg (0.0123 M⊕)\n" \
           f"半径: 1,737 km (0.273 R⊕)\n" \
           f"密度: 3.344 g/cm³\n" \
           f"表面重力: 1.62 m/s² (0.165 g)\n" \
           f"逃逸速度: 2.38 km/s\n\n" \
           f"**轨道参数:**\n" \
           f"半长轴: 384,400 km\n" \
           f"轨道周期: 27.32 天 (恒星月)\n" \
           f"同步自转周期: 27.32 天\n" \
           f"轨道离心率: 0.0549\n" \
           f"轨道倾角: 5.145°\n\n" \
           f"**表面特征:**\n" \
           f"表面温度: -173°C 到 127°C\n" \
           f"月球正面总有朝向地球（潮汐锁定）"


def _get_earth_info() -> str:
    """获取地球信息"""
    return f"🌍 地球 (Earth)\n\n" \
           f"**基本参数:**\n" \
           f"质量: 5.972×10²⁴ kg (1 M⊕)\n" \
           f"赤道半径: 6,378 km (1 R⊕)\n" \
           f"极半径: 6,357 km\n" \
           f"平均密度: 5.514 g/cm³\n" \
           f"表面重力: 9.807 m/s² (1 g)\n" \
           f"逃逸速度: 11.2 km/s\n\n" \
           f"**轨道参数:**\n" \
           f"半长轴: 1 AU = 1.496×10⁸ km\n" \
           f"轨道周期: 365.25 天\n" \
           f"自转周期: 23小时56分4秒\n" \
           f"轨道离心率: 0.0167\n" \
           f"轨道倾角: 0° (定义)\n" \
           f"自转轴倾角: 23.44°\n\n" \
           f"**大气组成:**\n" \
           f"氮: 78%\n" \
           f"氧: 21%\n" \
           f"氩和其他: 1%"


def _get_mercury_info() -> str:
    """获取水星信息"""
    return f"☿️ 水星 (Mercury)\n\n" \
           f"**基本参数:**\n" \
           f"质量: 3.30×10²³ kg (0.055 M⊕)\n" \
           f"半径: 2,440 km (0.383 R⊕)\n" \
           f"密度: 5.427 g/cm³\n" \
           f"表面重力: 3.7 m/s² (0.38 g)\n\n" \
           f"**轨道参数:**\n" \
           f"半长轴: 0.387 AU\n" \
           f"轨道周期: 87.97 天\n" \
           f"自转周期: 58.65 天\n" \
           f"轨道离心率: 0.206 (太阳系最大)\n\n" \
           f"**特点:**\n" \
           f"表面温度: -173°C 到 427°C\n" \
           f"几乎没有大气\n" \
           f"表面有大量陨石坑"


def _get_venus_info() -> str:
    """获取金星信息"""
    return f"♀️ 金星 (Venus)\n\n" \
           f"**基本参数:**\n" \
           f"质量: 4.87×10²⁴ kg (0.815 M⊕)\n" \
           f"半径: 6,052 km (0.949 R⊕)\n" \
           f"密度: 5.243 g/cm³\n" \
           f"表面重力: 8.87 m/s² (0.91 g)\n\n" \
           f"**轨道参数:**\n" \
           f"半长轴: 0.723 AU\n" \
           f"轨道周期: 224.7 天\n" \
           f"自转周期: 243 天 (逆向)\n" \
           f"轨道离心率: 0.007 (最接近圆形)\n\n" \
           f"**特点:**\n" \
           f"表面温度: ~462°C (太阳系最热)\n" \
           f"浓厚的CO₂大气\n" \
           f"表面气压: 92个地球大气压\n" \
           f"强烈的温室效应"


def _get_mars_info() -> str:
    """获取火星信息"""
    return f"♂️ 火星 (Mars)\n\n" \
           f"**基本参数:**\n" \
           f"质量: 6.42×10²³ kg (0.107 M⊕)\n" \
           f"半径: 3,390 km (0.532 R⊕)\n" \
           f"密度: 3.934 g/cm³\n" \
           f"表面重力: 3.71 m/s² (0.38 g)\n\n" \
           f"**轨道参数:**\n" \
           f"半长轴: 1.524 AU\n" \
           f"轨道周期: 687 天\n" \
           f"自转周期: 24.6 小时\n" \
           f"轨道离心率: 0.093\n" \
           f"自转轴倾角: 25.19°\n\n" \
           f"**特点:**\n" \
           f"表面温度: -140°C 到 20°C\n" \
           f"稀薄的CO₂大气\n" \
           f"有两颗小卫星: 火卫一和火卫二\n" \
           f"表面有极冠和古河道"


def _get_jupiter_info() -> str:
    """获取木星信息"""
    return f"♃ 木星 (Jupiter)\n\n" \
           f"**基本参数:**\n" \
           f"质量: 1.90×10²⁷ kg (318 M⊕)\n" \
           f"赤道半径: 71,492 km (11.2 R⊕)\n" \
           f"密度: 1.326 g/cm³\n" \
           f"表面重力: 24.79 m/s² (2.53 g)\n\n" \
           f"**轨道参数:**\n" \
           f"半长轴: 5.204 AU\n" \
           f"轨道周期: 11.86 年\n" \
           f"自转周期: 9.93 小时 (最快)\n" \
           f"轨道离心率: 0.049\n\n" \
           f"**特点:**\n" \
           f"气态巨行星\n" \
           f"大红斑 - 巨型风暴\n" \
           f"已知卫星: 95颗+\n" \
           f"主要卫星: 木卫一、二、三、四(伽利略卫星)\n" \
           f"强大的磁场"


def _get_saturn_info() -> str:
    """获取土星信息"""
    return f"♄ 土星 (Saturn)\n\n" \
           f"**基本参数:**\n" \
           f"质量: 5.68×10²⁶ kg (95.2 M⊕)\n" \
           f"赤道半径: 60,268 km (9.45 R⊕)\n" \
           f"密度: 0.687 g/cm³ (最低)\n" \
           f"表面重力: 10.44 m/s² (1.07 g)\n\n" \
           f"**轨道参数:**\n" \
           f"半长轴: 9.537 AU\n" \
           f"轨道周期: 29.46 年\n" \
           f"自转周期: 10.66 小时\n" \
           f"轨道离心率: 0.056\n\n" \
           f"**特点:**\n" \
           f"壮观的行星环系统\n" \
           f"已知卫星: 146颗+\n" \
           f"最大卫星: 土卫六(泰坦)，有浓厚大气\n" \
           f"密度小于水"


def _get_uranus_info() -> str:
    """获取天王星信息"""
    return f"♅ 天王星 (Uranus)\n\n" \
           f"**基本参数:**\n" \
           f"质量: 8.68×10²⁵ kg (14.5 M⊕)\n" \
           f"赤道半径: 25,559 km (4.01 R⊕)\n" \
           f"密度: 1.270 g/cm³\n" \
           f"表面重力: 8.69 m/s² (0.89 g)\n\n" \
           f"**轨道参数:**\n" \
           f"半长轴: 19.19 AU\n" \
           f"轨道周期: 84.02 年\n" \
           f"自转周期: 17.24 小时 (逆向)\n" \
           f"轨道离心率: 0.046\n" \
           f"自转轴倾角: 97.77° (几乎侧躺)\n\n" \
           f"**特点:**\n" \
           f"冰巨星\n" \
           f"蓝绿色(甲烷大气)\n" \
           f"已知卫星: 27颗\n" \
           f"有暗淡的行星环"


def _get_neptune_info() -> str:
    """获取海王星信息"""
    return f"♆ 海王星 (Neptune)\n\n" \
           f"**基本参数:**\n" \
           f"质量: 1.02×10²⁶ kg (17.1 M⊕)\n" \
           f"赤道半径: 24,764 km (3.88 R⊕)\n" \
           f"密度: 1.638 g/cm³\n" \
           f"表面重力: 11.15 m/s² (1.14 g)\n\n" \
           f"**轨道参数:**\n" \
           f"半长轴: 30.07 AU\n" \
           f"轨道周期: 164.8 年\n" \
           f"自转周期: 16.11 小时\n" \
           f"轨道离心率: 0.009\n\n" \
           f"**特点:**\n" \
           f"冰巨星\n" \
           f"深蓝色(甲烷大气)\n" \
           f"太阳系风速最快的行星\n" \
           f"已知卫星: 14颗\n" \
           f"最大卫星: 海卫一(特里同)，逆向轨道"


# 太阳系天体字典
SOLAR_SYSTEM = {
    'sun': _get_sun_info,
    'moon': _get_moon_info,
    'earth': _get_earth_info,
    'mercury': _get_mercury_info,
    'venus': _get_venus_info,
    'mars': _get_mars_info,
    'jupiter': _get_jupiter_info,
    'saturn': _get_saturn_info,
    'uranus': _get_uranus_info,
    'neptune': _get_neptune_info,
}
