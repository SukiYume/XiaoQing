# CHIME FRB 插件

CHIME FRB 重复暴监测插件，提供 CHIME FRB 重复暴观测数据的获取和监测功能。

## 功能介绍

监测和查询 CHIME 望远镜观测到的快速射电暴（FRB）重复暴数据，支持实时更新和历史数据查询。

## 使用方法

### 基本命令

```
/chime                    # 获取最新的重复暴数据
/chime list               # 列出最近更新的 FRB
/chime <FRB名称>          # 查询指定 FRB 的详细信息
/chime help               # 显示帮助信息
```

### 示例

```
/chime                    # 获取所有重复暴最新状态
/chime list               # 列出最近更新的 5 个 FRB
/chime FRB20180916B       # 查询 FRB20180916B 的详细信息
/chime help               # 显示帮助
```

## 配置说明

无需额外配置，开箱即用。

插件会自动：
- 缓存 FRB 数据
- 定期检查更新
- 记录新观测脉冲

## 功能特性

- 实时获取 CHIME FRB 重复暴目录数据
- 自动检测新观测脉冲
- 展示 FRB 的详细参数（DM、SNR、时间戳等）
- 支持指定 FRB 查询
- 数据本地缓存

## 数据来源

- CHIME/FRB Collaboration
- API: https://catalog.chime-frb.ca/repeaters

## 显示信息

每个 FRB 包含以下信息：
- FRB 名称
- 最新脉冲日期
- 时间戳（MJD）
- 色散量（DM）
- 信噪比（SNR）
- 总脉冲数量

## 注意事项

- 数据来自 CHIME 官方目录
- 需要网络连接获取最新数据
- 仅包含重复暴 FRB（非一次性事件）
- 数据会在本地缓存以提高性能
