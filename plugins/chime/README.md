# 📡 CHIME/FRB

`chime` 查询 CHIME/FRB 重复暴目录，并按本地通知基线发现新增重复暴和新脉冲。

---

## ⌨️ 命令

<!-- manifest-command-aliases:start -->
| 功能 | 推荐入口 | Manifest 等价别名 |
| --- | --- | --- |
| 查询与检查重复暴 | `/chime` | `/frb` |
<!-- manifest-command-aliases:end -->

| 用法 | 结果 |
| --- | --- |
| `/chime` | 预览上次成功通知以来的目录更新 |
| `/chime list` | 按观测时间列出最近更新的 5 个 FRB |
| `/chime FRB20180916B` | 查询指定 FRB 的时间、DM、RA、DEC 和 SNR |
| `/chime help` | 显示本地帮助 |

命令接受一个子命令或一个规范 FRB 名称。`列表` 和 `帮助` 分别是 `list` 和 `help` 的中文别名。

---

## ⏰ 定时通知

清单在每天 09:00 和 21:00 以 Core `targeted` 模式运行 `scheduled_check`，通知目标来自 Core 为该 schedule 解析出的群列表。一次检查按以下顺序执行：

1. 读取 `chime_delivery.json`，优先续发已有待办通知；
2. 获取并校验远端目录；
3. 计算相对通知基线的新增记录；
4. 按目标群发送消息，并逐个原子记录确认状态；
5. 全部目标都有结论后更新 `chime_history.json`，随后清理待办通知。

“有结论”包括明确送达和结果未知：公告类通知按 at-most-once 处理，OneBot 已接收但没有回执的目标同样记为完成，避免重复公告，代价是这类目标可能漏收。明确失败的目标保留待办，下一轮续发。

该流程支持进程重启后的断点续发。网络发送成功与本地确认落盘之间若断电，单个目标仍可能再次收到同一条通知。

手动查询复用 5 分钟目录缓存，并发冷启动共享同一次下载。定时检查每次获取新目录。

---

## 💾 数据来源

运行时目录接口：

```text
https://catalog.chime-frb.ca/repeaters
```

2026-09-05 的运行记录显示，旧接口出现 TLS 证书域名不匹配。请求在目录解析前失败。当时的[官方公共数据页](https://www.chime-frb.ca/repeaters)公告网站正在重建基础设施，并继续提供已发表目录归档。这是带日期的历史记录。接口当前可用性需要新的运行检查。

实时查询与增量通知需要该接口恢复，或由官方提供等价接口。固定发布归档具有明确的数据截止时间。获取失败时，插件保留证书校验和通知基线，并返回获取失败提示。

发布数据集可从以下官方入口获取：

- [CHIME/FRB Catalog](https://www.chime-frb.ca/catalog)
- [CHIME/FRB Open Data](https://chime-frb-open-data.github.io/)

---

## 💾 本地状态

| 文件 | 内容 |
| --- | --- |
| `data/chime/chime_history.json` | 每个 FRB 最近一次已确认通知的时间戳 |
| `data/chime/chime_delivery.json` | 待办通知、目标群和逐目标确认状态 |

插件会严格校验两个文件的结构、记录数量、名称和时间戳。目录异常、状态文件异常、发送异常和空目标列表都会保留现有通知基线，供维护者修复后继续处理。

---

## 🔐 输入与网络边界

- 脉冲日期键采用完整 6 位日期；
- FRB 名称经过格式、长度与控制字符校验；
- 观测时间采用包含日期和秒的 ISO 风格字符串；
- 外部响应执行正文大小、JSON 深度、节点数与字段类型限制；
- 日志使用经过边界处理的名称和错误码。

---

## 🩺 排障

| 现象 | 检查项 |
| --- | --- |
| 目录暂时可用性异常 | 检查远端接口、网络连接和运行日志 |
| 定时消息持续待办 | 检查目标群配置、发送权限与 `chime_delivery.json` |
| 状态文件校验失败 | 从备份恢复对应 JSON，或由维护者确认基线后重建 |
| 查询结果为空 | 核对 FRB 名称与目录响应时间 |

---

## ✅ 开发验证

在仓库根目录运行：

```bash
python -m ruff check plugins/chime tests/plugins/chime/test_chime.py
python -m mypy plugins/chime
python -m pytest -q tests/plugins/chime/test_chime.py \
  tests/plugins/contracts/test_durable_plugin_notifications.py \
  tests/plugins/contracts/test_fixed_origin_http_clients.py
```
