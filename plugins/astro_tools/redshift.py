"""
红移计算模块
"""


async def handle_redshift(args: str, context) -> str:
    """处理红移计算命令"""
    args = args.strip()
    if not args:
        return "请提供红移值\n示例: /astro redshift 0.5\n\n" \
               "红移范围: 0 到 ~10 (宇宙学红移)\n" \
               "使用 Planck 2018 宇宙学参数"
    
    try:
        # 验证输入
        try:
            z = float(args)
        except ValueError:
            return f"无效的红移值: {args}\n请提供有效的数字"
        
        if z < 0:
            return "红移值必须 >= 0\n\n" \
                   "注: z=0 表示当前宇宙，z>0 表示过去的宇宙"
        
        if z > 1100:
            return f"红移值 {z} 过大\n\n" \
                   f"注: 宇宙微波背景辐射的红移约为 z≈1100\n" \
                   f"观测到的最远星系红移约为 z≈13"
        
        from astropy.cosmology import Planck18 as cosmo
        from astropy import units as u
        
        # 计算各种距离和时间
        d_L = cosmo.luminosity_distance(z)
        d_A = cosmo.angular_diameter_distance(z)
        d_C = cosmo.comoving_distance(z)
        t_lookback = cosmo.lookback_time(z)
        age_at_z = cosmo.age(z)
        
        result = f"🌌 红移计算 (Planck 2018)\n\n"
        result += f"**输入红移: z = {z}**\n\n"
        result += f"**距离:**\n"
        
        # 根据距离大小选择合适的单位
        if d_L.value < 1:
            result += f"光度距离: {d_L.to(u.Mpc):.3f}\n"
            result += f"角直径距离: {d_A.to(u.Mpc):.3f}\n"
            result += f"共动距离: {d_C.to(u.Mpc):.3f}\n"
        else:
            result += f"光度距离: {d_L.to(u.Gpc):.4f}\n"
            result += f"角直径距离: {d_A.to(u.Gpc):.4f}\n"
            result += f"共动距离: {d_C.to(u.Gpc):.4f}\n"
        
        result += f"\n**时间:**\n"
        result += f"光行时: {t_lookback.to(u.Gyr):.3f}\n"
        result += f"宇宙年龄(当时): {age_at_z.to(u.Gyr):.3f}\n"
        
        result += f"\n**其他参数:**\n"
        result += f"尺度因子: a = 1/(1+z) = {1/(1+z):.6f}\n"
        
        # 添加物理意义说明
        if z < 0.1:
            result += f"\n💡 近邻宇宙 - 可用于局部星系研究"
        elif z < 1:
            result += f"\n💡 中等红移 - 星系演化的重要阶段"
        elif z < 3:
            result += f"\n💡 高红移 - 星系形成活跃时期"
        else:
            result += f"\n💡 极高红移 - 早期宇宙"
        
        return result
    except ValueError as e:
        return f"计算错误: {e}"
    except Exception as exc:
        return f"红移计算失败: {exc}"
