# 天文工具箱

统一入口为 `/astro <子命令>`。当前实现和测试覆盖的子命令如下：

- `time`：ISO、JD、MJD、Unix 时间戳和当前时间转换
- `coord`：ICRS 赤道坐标、Galactic 和 geocentric true ecliptic 的 Astropy 转换
- `convert`：Jy/mJy、pc/ly/AU、长度、频率、温度和能量单位
- `redshift`：使用 Astropy `Planck18` 模型计算 `0 ≤ z ≤ 1100` 的距离与宇宙年龄
- `formula`：查看或计算已实现的公式
- `obj`：太阳系内置信息或 SIMBAD 对象查询
- `const`：天文和物理常数

示例：

```text
/astro time now
/astro time unix 1706616000
/astro coord 12:30:00 +15:00:00
/astro coord galactic 120 30
/astro convert 1 pc ly
/astro obj M31
/astro const ly
```

当前没有 `/astro object` 别名，也没有通用 FK4、任意 epoch/equinox 转换接口。SIMBAD 查询有客户端 timeout 和总 deadline。
