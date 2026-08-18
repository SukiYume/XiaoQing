<div align="center">

# 🌿 XiaoQingBot

**基于 OneBot v11 和 Python asyncio 的插件化 QQ 助手**

[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?style=flat-square&logo=python&logoColor=white)](https://python.org)
[![License](https://img.shields.io/badge/License-MIT-green?style=flat-square)](LICENSE)
[![OneBot](https://img.shields.io/badge/OneBot-v11-black?style=flat-square)](https://onebot.dev/)
[![Documentation](https://img.shields.io/badge/Documentation-online-2ea44f?style=flat-square)](https://paris.escape.ac.cn/note/XiaoQing/)

`QQ / OneBot 事件 → XiaoQing Core → 插件路由与会话 → OneBot 消息回复`

</div>

---

## ✨ 项目简介

XiaoQingBot 面向长期运行的私人 QQ 助手场景。Core 负责 OneBot 接入、命令路由、会话、调度、配置、插件生命周期和运行指标；插件提供聊天、个人管理、天文工具、远程操作、内容解析与娱乐功能。

两个核心插件覆盖主要日常场景：

- `xiaoqing_chat`：多模态拟人聊天，支持群聊参与、私聊、图片、表情、引用、长期记忆、行为规划和表达学习。
- `pendo`：个人时间与信息管理，支持日程、待办、笔记、日记、账本、提醒、搜索、统计、Web 控制台和 iPhone Scriptable 小组件。

XiaoQing 面向回环地址、可信内网或严格反向代理保护的私人服务。插件采用受信任代码模型，与 Bot 共享 Python 进程和操作系统权限。请安装经过审查的第一方插件或可信插件。

---

## ✨ 核心能力

| 能力 | 当前实现 |
|---|---|
| OneBot v11 接入 | 被动 HTTP/WebSocket Inbound，以及主动 WebSocket Client |
| 异步运行时 | Core 收发、调度、HTTP 和插件入口基于 `asyncio` |
| 插件系统 | 每个插件拥有独立 Manifest、配置命名空间、数据目录和生命周期 |
| 执行治理 | 插件入口与同步任务使用有界队列、按插件并发控制和公平调度 |
| 命令目录 | Manifest 统一提供命令层级、别名、权限、场景、样例和帮助内容 |
| 多轮会话 | 按会话键串行、快照隔离、事务提交，适合表单、游戏、REPL 和 SSH |
| 后台任务 | 插件通过主动发送能力回传长任务的文字、图片和文件结果 |
| 定时任务 | 插件在 Manifest 中声明 cron 任务，由 Core 统一调度 |
| 配置管理 | 配置快照、敏感配置保护、文件监视和运行时重载 |
| 运行观测 | 日志、请求 ID、插件错误码和 `/metrics` 指标 |

架构与消息流详见 [系统架构](docs/02-architecture.md) 和 [消息处理流程](docs/08-message-flow.md)。

---

## 🚀 快速开始

### 1. 准备环境

- Python `3.10+`
- 一个 OneBot v11 实现，推荐 [NapCatQQ](https://github.com/NapNeko/NapCatQQ)
- 可访问 QQ 与所需第三方服务的网络环境

根目录 `requirements.txt` 包含 Core、默认插件和 Pendo Web 的直接依赖。Jupyter 内核与 arXiv 本地模型按对应插件文档准备。

### 2. 安装项目

以下命令适用于 Git Bash、macOS 和 Linux：

```bash
git clone https://github.com/SukiYume/XiaoQing.git
cd XiaoQing
python -m pip install -r requirements.txt
```

### 3. 创建配置

```bash
cp config/config.json.example config/config.json
cp config/secrets.json.example config/secrets.json
```

最小 `config/config.json` 示例：

```json
{
  "bot_name": "小青",
  "command_prefixes": ["/"],
  "require_bot_name_in_group": true,
  "enable_ws_client": false,
  "enable_inbound_server": true,
  "onebot_http_base": "http://127.0.0.1:11001",
  "inbound_http_base": "http://127.0.0.1:12000",
  "inbound_trusted_tls_proxy": false,
  "timezone": "Asia/Shanghai",
  "log_level": "INFO",
  "plugins": {
    "smalltalk_provider": "xiaoqing_chat"
  }
}
```

最小 `config/secrets.json` 示例：

```json
{
  "inbound_token": "your-secret-token",
  "admin_user_ids": [123456789]
}
```

将 token、管理员 QQ、LLM API Key 和第三方服务密钥保存在 `config/secrets.json`。Git 忽略规则覆盖本机正式配置文件。

完整字段、AI 模型注册表和插件配置见 [配置详解](docs/06-configuration.md)。

### 4. 连接 OneBot

被动 Inbound 模式由 OneBot 向 XiaoQing 推送事件。NapCat HTTP POST 示例：

```yaml
http:
  post:
    - url: http://127.0.0.1:12000/event
      secret: your-secret-token
```

主动 WebSocket 模式由 XiaoQing 连接 OneBot：

```json
{
  "enable_ws_client": true,
  "enable_inbound_server": false,
  "onebot_ws_uri": "ws://127.0.0.1:11000/ws"
}
```

Inbound 默认绑定 loopback。启用 HTTP 或 WebSocket Inbound 时，请设置非空 `inbound_token`。公网入口由 Nginx 或 Caddy 终止 TLS，再转发到 loopback。受控跨容器网络可配合防火墙启用 `inbound_trusted_tls_proxy`。完整边界见 [Inbound 配置](docs/06-configuration.md#inbound-网络边界)。

### 5. 启动与验证

```bash
python main.py
```

启动日志会列出已加载插件和 OneBot 连接状态。随后在 QQ 中依次发送：

```text
/help
/plugins
/metrics
/help xiaoqing_chat
/help pendo
/xc 你好
/pendo
```

---

## ⌨️ 命令帮助

帮助系统按“功能域 → 插件 → 子命令 → 具体命令”逐层展开，适合手机阅读：

```text
/help
/help pendo
/help pendo todo
/help pendo todo add
/help json pendo
```

常用 Core 命令：

| 命令 | 作用 |
|---|---|
| `/help [查询] [page N]` | 浏览或搜索命令目录 |
| `/help json [查询] [page N]` | 导出结构化命令目录 |
| `/plugins` | 查看已加载插件 |
| `/reload` | 重新扫描配置与插件 |
| `/闭嘴 [时长]` | 暂停当前群的普通聊天回复 |
| `/说话` | 恢复当前群的普通聊天回复 |
| `/metrics` | 查看运行指标 |

全部插件命令见 [插件功能介绍](docs/09-plugins.md)。

---

## 🧩 主要插件

### xiaoqing_chat

`xiaoqing_chat` 作为全局 `smalltalk_provider` 观察群聊与私聊消息，并结合 attention gate、频率控制、行为规划、主模型和 reply checker 决定回复内容与时机。它支持文本、图片、QQ face、NapCat mface、引用消息、本地媒体回复、长期记忆和表达学习。

- [使用说明](plugins/xiaoqing_chat/README.md)
- [架构说明](plugins/xiaoqing_chat/ARCHITECTURE.md)

### Pendo

Pendo 通过统一数据模型管理日程、待办、笔记、日记、账本和提醒。聊天命令、Web 控制台与 Scriptable 小组件共享同一份 SQLite 数据。

浏览器登录码的兑换期为 7 天，兑换后建立 7 天 HttpOnly Cookie 会话。Scriptable 小组件使用默认 365 天的只读 Bearer Token。两类凭据统一采用秒级期限和摘要存储。

- [使用说明](plugins/pendo/README.md)
- [架构说明](plugins/pendo/ARCHITECTURE.md)
- [Scriptable 小组件](docs/pendo-scriptable-widget.md)

### Codex 与 arXiv

`codex` 维护独立的后台会话和任务队列。同一标签内任务串行执行，各标签按 `max_parallel_jobs` 并行执行，完成结果通过 OneBot 主动回传。

`arxiv_filter` 发送当日筛选结果，并将 positive 论文交给 Codex `astro-ph` 会话生成摘要。源列表日期和规范化论文链接集合共同标识一次摘要任务。

- [Codex 插件](plugins/codex/README.md)
- [arXiv Filter 插件](plugins/arxiv_filter/README.md)

### Flickr

`flickr` 通过 Flickr 官方 REST API 浏览今日精选、关键词与标签搜索、Flickr Commons、用户公开照片和公开相册。搜索可按许可、排序与拍摄日期筛选；回复保留作者、许可条件和 Flickr 照片页，一次查询可用 `/flickr more` 连续浏览。

- [Flickr 插件](plugins/flickr/README.md)

---

## 💾 插件与数据目录

项目主要目录：

```text
XiaoQing/
├── main.py                  # 进程入口
├── config/                  # 配置示例与本机配置
├── core/                    # Core 运行时
├── plugins/                 # 插件源码
├── data/                    # 插件运行数据
├── docs/                    # 项目文档
├── scripts/                 # 启动、同步和 UAT 脚本
└── tests/                   # 自动化测试
```

插件运行数据默认位于 `data/<plugin_name>/`。`data_root` 可配置项目级数据根目录。源码、配置和运行数据采用独立目录，部署脚本可据此同步代码并保留生产数据。

每个插件目录以 `plugin.json` 描述入口、命令、权限、场景、依赖、文件监视和定时任务。插件名使用小写 ASCII Python 标识符，入口使用插件目录内的规范 POSIX `.py` 相对路径。

插件开发流程见 [插件开发指南](docs/03-plugin-development.md) 和 [API 参考](docs/05-api-reference.md)。

---

## ⚙️ 配置与运行维护

配置分为两类：

- `config/config.json`：OneBot 地址、命令前缀、日志、AI 模型、插件设置和执行限制。
- `config/secrets.json`：Inbound token、管理员 QQ、AI provider 密钥和第三方服务凭据。

常用重载命令：

```text
/reload
```

普通配置采用 last-known-good 快照，敏感配置采用 fail-closed 快照。运行中的管理员可通过 `/set_secret` 更新 `secrets.json` 已有路径；Core 在同一事务中保存文件并发布新 revision。部署工具直接替换 `config.json` 或 `secrets.json` 时，先停止 Bot、写入完整文件，再重新启动，使两个来源在启动阶段组成已确认 revision。独立受保护的 Inbound 仍可用时，也可通过该通道执行 `/reload` 确认稳定来源。

Windows 生产启动链为 `scripts/run-bot.vbs → scripts/run-bot-monitor.ps1 → scripts/run_process_with_rotating_logs.py`。双击 `scripts/stop-bot.vbs` 可安全停止同一仓库的监控器、Bot 与 NapCat；由提升权限的 SSH、计划任务或管理员终端启动进程时，停服入口会显示一次 UAC 确认。完成提示出现后可再次双击 `scripts/run-bot.vbs` 启动。同步脚本、启动参数和生产目录说明位于 `scripts/` 对应文件的注释与帮助输出中。

---

## ✅ 开发与测试

常用门禁：

```bash
python -m compileall -q core plugins tests
python -m pytest -n 2
python -m ruff check .
python -m ruff format --check .
python -m mypy core plugins
git diff --check
```

完整上线验收：

```bash
bash scripts/run_full_uat.sh --plan-only
bash scripts/run_full_uat.sh
```

阶段说明、报告位置和外部场景选项见 [快速开始的完整 UAT](docs/01-getting-started.md#完整-uat)。

---

## 📚 文档导航

| 文档 | 内容 |
|---|---|
| [在线文档](https://paris.escape.ac.cn/note/XiaoQing/) | 适合浏览器阅读的项目手册 |
| [文档目录](docs/README.md) | 阅读路线和主题索引 |
| [项目概览](docs/00-overview.md) | 能力、概念和目录结构 |
| [快速开始](docs/01-getting-started.md) | 安装、配置、接入和首次验证 |
| [系统架构](docs/02-architecture.md) | 组件职责、服务边界和数据流 |
| [插件开发](docs/03-plugin-development.md) | Manifest、入口、会话、调度和测试 |
| [Core 模块](docs/04-core-modules.md) | `core/` 模块职责 |
| [API 参考](docs/05-api-reference.md) | PluginContext、消息段和公开 API |
| [配置详解](docs/06-configuration.md) | 配置、secrets、部署和热重载 |
| [高级主题](docs/07-advanced.md) | 会话、调度、权限和 Web 插件 |
| [消息流程](docs/08-message-flow.md) | 事件接收到回复发送的完整链路 |
| [插件功能](docs/09-plugins.md) | 内置插件的命令与配置入口 |
| [更新记录](CHANGELOG.md) | 版本变化和升级影响 |

---

## 🩺 故障排查

### 启动消息响应排查

1. 确认 OneBot 进程和 XiaoQing 进程均处于运行状态。
2. 确认 Inbound URL 或主动 WebSocket URI 与双方配置一致。
3. 确认 OneBot secret 与 `inbound_token` 一致。
4. 查看日志中的连接状态、错误码和 request ID。
5. 日志出现 `secrets source is inconsistent` 与 `WebSocket client stopped` 时，按停服、保存完整配置来源、重新启动的顺序恢复已确认凭据 revision。

### 群聊缺少普通聊天回复

1. 确认 `plugins.smalltalk_provider` 指向目标聊天插件。
2. 查看当前群的静音状态。
3. 查看 `xiaoqing_chat` attention、频率控制和行为规划日志。

### Pendo Web 登录页持续出现

1. 执行 `/pendo web status` 确认服务地址。
2. 执行 `/pendo web token` 获取登录码。
3. 在登录页兑换登录码并允许浏览器保存 Cookie。
4. 查看 Pendo Web 日志中的会话校验结果。

### AI 插件调用异常

1. 核对 `config.ai.providers`、`config.ai.models` 与插件 route 的引用。
2. 核对 `secrets.ai.providers.<provider>.api_key`。
3. 检查代理、网络、配额和 provider 状态。
4. 结合插件日志中的 route、profile 和错误码定位请求。

---

## 📌 许可证

本项目采用 [MIT License](LICENSE)。
