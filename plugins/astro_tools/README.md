# Astro Tools 插件

天文工具插件，提供各种天文计算和转换功能。

## 功能介绍

Astro Tools 是一个综合性的天文工具集，提供坐标转换、时间计算、红移转换、天体信息查询、公式计算等功能。

## 使用方法

### 基本命令

```
/astro <子命令> [参数]
/astro help
```

### 可用子命令

#### 坐标转换 (coord)
```
/astro coord <RA> <Dec> <epoch>     # 坐标转换
/astro coord help                    # 坐标帮助
```

#### 时间转换 (time)
```
/astro time <时间> <格式>            # 时间转换
/astro time now                      # 当前时间
/astro time help                     # 时间帮助
```

#### 红移计算 (redshift)
```
/astro redshift <z>                  # 计算红移对应的距离
/astro redshift help                 # 红移帮助
```

#### 天体查询 (object)
```
/astro object <天体名称>             # 查询天体信息
/astro object help                   # 天体帮助
```

#### 单位转换 (convert)
```
/astro convert <值> <源单位> <目标单位>
/astro convert help                  # 转换帮助
```

#### 公式计算 (formula)
```
/astro formula <公式名称> [参数]
/astro formula list                  # 列出所有公式
/astro formula help                  # 公式帮助
```

## 功能特性

### 坐标转换
- FK5/FK4 坐标系转换
- J2000/B1950 历元转换
- 赤道坐标和银道坐标互转
- 角距离计算

### 时间转换
- UTC, JD, MJD 互转
- LST 恒星时计算
- 时区转换

### 红移计算
- 光度距离
- 角直径距离
- 共动距离
- 宇宙年龄

### 天体查询
- 基于 Simbad 数据库
- 天体坐标
- 天体类型
- 视星等
- 红移

### 单位转换
- 长度单位（pc, kpc, Mpc, ly, AU, km）
- 角度单位（deg, arcmin, arcsec, rad）
- 质量单位（Msun, Mjup, Mearth, kg）
- 能量单位（erg, eV, J）

### 公式计算
- 史瓦西半径
- 洛伦兹因子
- 爱丁顿光度
- 热辐射
- 更多...

## 配置说明

无需特殊配置，开箱即用。

天体查询功能需要网络连接访问 Simbad 数据库。

## 示例

```
/astro coord 12:34:56.7 +12:34:56.7 J2000
/astro time now
/astro redshift 0.5
/astro object M31
/astro convert 1 Mpc pc
/astro formula schwarzschild 10
/astro help
```

## 依赖

- astropy (天文计算库)
- astroquery (天体查询库)

安装依赖：
```bash
pip install astropy astroquery
```

## 注意事项

- 坐标转换需要正确的格式
- 天体查询需要网络连接
- 某些计算可能需要额外的宇宙学参数
- 大数值计算注意单位选择

## 适用场景

- 天文数据处理
- 坐标系转换
- 快速天体信息查询
- 红移和距离计算
- 单位换算
- 理论公式计算

## 参考

- Astropy Documentation: https://www.astropy.org/
- Simbad Database: http://simbad.u-strasbg.fr/
