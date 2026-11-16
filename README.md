<div align="center">

# 🌿 XiaoQingBot

[English](README.en.md)

<img src="docs/assets/xiaoqing-full-body.png" alt="小青的全身立绘：灰紫短发、蓝色开衫，抱着观测笔记挥手的天文专业大二女生" width="300">

**小青 · 快逃科学技术研究院**

**基于 OneBot v11 和 Python asyncio 的插件化 QQ 助手**

[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?style=flat-square&logo=python&logoColor=white)](https://python.org)
[![License](https://img.shields.io/badge/License-MIT-green?style=flat-square)](LICENSE)
[![OneBot](https://img.shields.io/badge/OneBot-v11-black?style=flat-square)](https://onebot.dev/)
[![Documentation](https://img.shields.io/badge/Documentation-online-2ea44f?style=flat-square)](https://paris.escape.ac.cn/note/XiaoQing/)

`QQ / OneBot 事件 → XiaoQing Core → 插件路由与会话 → OneBot 消息回复`

</div>

---

## ✨ 项目简介

XiaoQingBot 为私人 QQ 助手提供长期运行能力。Core 负责 OneBot 接入、命令路由、会话、调度、配置、插件生命周期和运行指标。插件提供聊天、个人管理、天文工具、远程操作、内容解析与娱乐功能。

两个核心插件覆盖主要日常场景：

- `xiaoqing_chat`：支持群聊、私聊、图片、表情、引用、长期记忆、行为规划和表达学习。开启 `xiaoqing_agent` 后，工具执行遵循发起人的权限。
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
| 权限 | 谁能在哪里用哪条命令只在 Manifest 声明，Core 用同一处判断命令、会话和派生能力 |
| 多轮会话 | 按会话键串行、快照隔离、事务提交，适合表单、游戏、REPL 和 SSH |
| 后台任务 | 插件通过主动发送能力回传长任务的文字、图片和文件结果 |
| 定时任务 | Manifest 声明 cron、投递模式与群目标，Core 统一调度、校验和发送 |
| 配置管理 | 配置快照、敏感配置保护、文件监视和运行时重载 |
| 运行观测 | 日志、请求 ID、插件错误码和 `/metrics` 指标 |

架构与消息流详见 [系统架构](docs/02-architecture.md) 和 [消息处理流程](docs/08-message-flow.md)。

---

## 🚀 快速开始

### 1. 准备环境

- Python `3.11+`
- 一个 OneBot v11 实现，推荐 [NapCatQQ](https://github.com/NapNeko/NapCatQQ)
- 可访问 QQ 与所需第三方服务的网络环境

根目录 `requirements.txt` 包含 Core、默认插件、Pendo Web 和 Jupyter 内核的直接依赖。arXiv 本地推理的可选依赖与模型资产按 arXiv Filter 插件文档准备。

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

编辑复制出来的 `config/config.json`，核对以下字段：

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

示例文件中的 `default_group_ids` 是占位值 `123456789`，定时任务未指定群时向这些群发送消息，请改成自己的群号或留空 `[]`。

编辑复制出来的 `config/secrets.json`，至少设置以下字段：

```json
{
  "inbound_token": "your-secret-token",
  "admin_user_ids": [123456789]
}
```

将 token、管理员 QQ、LLM API Key 和第三方服务密钥保存在 `config/secrets.json`。Git 忽略规则覆盖本机正式配置文件。

完整字段、AI 模型注册表和插件配置见 [配置详解](docs/06-configuration.md)。

### 4. 连接 OneBot

最短路径是被动 Inbound：由 OneBot 向 XiaoQing 推送事件。在 NapCat 的 HTTP 事件上报端配置以下请求地址与请求头：

```http
POST /event HTTP/1.1
Host: 127.0.0.1:12000
Authorization: Bearer your-secret-token
Content-Type: application/json
```

上报端用 Bearer 请求头鉴权，token 就是 `inbound_token`；只配置 OneBot 的 `secret` 签名会收到 401。

同时开启 NapCat 的 HTTP API，使其监听 `onebot_http_base` 指向的端口（示例为 11001），回复经它发出。NapCat 设置了 access token 时，在 `secrets.json` 顶层配置相同值的 `onebot_token`；未设置则保持空字符串。

主动 WebSocket 接入、鉴权细节和公网部署的网络边界见 [快速开始的配置 OneBot](docs/01-getting-started.md#️-3-配置-onebot)。

### 5. 启动与验证

```bash
python main.py
```

配置读仓库下的 `config/`，数据和日志写到 `data/`、`logs/`。用 wheel 安装时运行 `xiaoqing`，这三个目录在当前目录下，也可以用 `xiaoqing --root <目录>` 指定。

启动日志会列出已加载插件和 OneBot 连接状态。随后用 `admin_user_ids` 中的 QQ 私聊小青，依次发送：

```text
/help
/plugins
/metrics
/help xiaoqing_chat
/help pendo
/xc 你好
/pendo
```

`/xc` 需要先按 [快速开始](docs/01-getting-started.md#ai-插件配置) 配好 AI 模型和密钥，否则无法生成回复。

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

| 命令 | 权限 | 作用 |
|---|---|---|
| `/help [查询] [page N]` | 公开 | 浏览或搜索命令目录 |
| `/help json [查询] [page N]` | 公开 | 导出结构化命令目录 |
| `/plugins` | 公开 | 查看已加载插件 |
| `/reload` | Bot 管理员 | 重载配置与插件，完成后通知结果 |
| `/闭嘴 [时长]` | Bot 管理员，群聊 | 暂停当前群的普通聊天回复，默认 10 分钟，最长 24 小时 |
| `/说话` | Bot 管理员，群聊 | 恢复当前群的普通聊天回复 |
| `/set_secret <路径> <值>` | Bot 管理员，私聊 | 修改 `secrets.json` 中已有的敏感配置 |
| `/get_secret <路径>` | Bot 管理员，私聊 | 查看脱敏后的值或对象键名 |
| `/metrics` | Bot 管理员 | 查看运行时间、成功率、错误和慢插件 |

全部插件的命令、别名、权限和定时任务见 [插件使用手册](docs/09-plugins.md#️-命令入口速查)。

---

## 🧩 主要插件

| 插件 | 作用 | 文档 |
|---|---|---|
| `xiaoqing_chat` | 拟人聊天：判断接话时机和对象，读图片和表情包，回复发出前经过审查 | [使用说明](plugins/xiaoqing_chat/README.md)、[架构](plugins/xiaoqing_chat/ARCHITECTURE.md) |
| `xiaoqing_agent` | 有人叫小青做事时调用命令和工具，权限跟随发起人；默认关闭 | [使用说明](plugins/xiaoqing_agent/README.md) |
| `xiaoqing_life` | 小青自己的网上生活：按兴趣上网，记下带来源的经历供聊天取用；默认关闭 | [使用说明](plugins/xiaoqing_life/README.md)、[架构](plugins/xiaoqing_life/ARCHITECTURE.md) |
| `xiaoqing_memory` | 统一记忆：聊天、办事和网上生活写进来，每晚整理成关于每个人、每个群和她自己的页面 | [使用说明](plugins/xiaoqing_memory/README.md)、[架构](plugins/xiaoqing_memory/ARCHITECTURE.md)、[设计](plugins/xiaoqing_memory/DESIGN.md) |
| `pendo` | 日程、待办、笔记、日记、账本和提醒，带 Web 控制台与 Scriptable 小组件 | [使用说明](plugins/pendo/README.md)、[架构](plugins/pendo/ARCHITECTURE.md)、[Scriptable 小组件](docs/pendo-scriptable-widget.md) |
| `codex` | 管理本机 Codex CLI 的后台会话和任务队列，结果主动发回 | [使用说明](plugins/codex/README.md) |
| `arxiv_filter` | 用本地兴趣模型筛选当日 arXiv 论文，可交给 Codex 生成摘要 | [使用说明](plugins/arxiv_filter/README.md) |
| `flickr` | 通过 Flickr 官方 API 浏览今日精选、搜索、Flickr Commons 和公开相册 | [使用说明](plugins/flickr/README.md) |

前四个插件合起来是小青这个角色的运行时，它们之间的分工见 [系统架构的小青运行时](docs/02-architecture.md#-小青运行时)。全部内置插件的开启方式、命令、配置和运行数据见 [插件使用手册](docs/09-plugins.md)。

---

## 💾 插件与数据目录

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

插件运行数据默认位于 `data/<plugin_name>/`，源码、配置和运行数据各在独立目录。每个插件目录以 `plugin.json` 描述入口、命令、权限、场景、依赖和定时任务。

目录与概念见 [项目概览](docs/00-overview.md)，插件开发流程见 [插件开发指南](docs/03-plugin-development.md) 和 [API 参考](docs/05-api-reference.md)。

---

## ⚙️ 配置与运行维护

配置分为两个文件：

- `config/config.json`：OneBot 地址、命令前缀、日志、AI 模型、插件设置和执行限制。
- `config/secrets.json`：Inbound token、管理员 QQ、AI provider 密钥和第三方服务凭据。

普通配置出错时继续使用最近一次有效的快照（last-known-good）；敏感配置缺失、损坏或与公开配置对不上时，Core 撤销凭据（fail-closed）。运行中修改配置有三种做法：

- 修改已有密钥：管理员私聊发送 `/set_secret <路径> <值>`。
- 外部工具改写 `secrets.json`：Core 私聊管理员列出变化的字段路径（不含值），核对后发送 `/reload` 应用。
- 修改 `config.json` 或两个文件一起替换：安排在停服窗口内；保留独立受保护 Inbound 的部署也可以在文件写好后经它执行 `/reload`。

每类配置的生效方式见 [配置热重载](docs/06-configuration.md#️-配置热重载)。Windows 下的受保护启动链和停服入口见 [快速开始的启动服务](docs/01-getting-started.md#-4-启动服务)。

---

## ✅ 开发与测试

常用门禁：

```bash
python -m compileall -q core plugins tests
python -m pytest -n 2
python -m ruff check .
python scripts/format_code.py --check
python -m mypy core plugins
git diff --check
```

完整上线验收：

```bash
python scripts/run_full_uat.py --plan-only
python scripts/run_full_uat.py
```

测试的组织方式和编写约定见 [插件开发指南的验证与文档](docs/03-plugin-development.md#验证与文档)。UAT 的阶段说明、报告位置和外部场景选项见 [快速开始的完整 UAT](docs/01-getting-started.md#完整-uat)。

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
| [插件使用手册](docs/09-plugins.md) | 内置插件的命令与配置入口 |
| [更新记录](CHANGELOG.md) | 版本变化和升级影响 |

---

## 🩺 故障排查

机器人没有回应时先看这几项：

1. OneBot 进程和 XiaoQing 进程都在运行。
2. Inbound URL 或主动 WebSocket URI 与双方配置一致。
3. 上报请求的 Bearer token 与 `inbound_token` 一致，回复通道的 `onebot_token` 正确。
4. 日志中的连接状态、错误码和 request ID。

群聊命令没有响应、普通聊天回复少、端口绑定、Pendo Web 登录、AI 调用异常和 secrets 待确认通知的处理，见 [快速开始的常见问题](docs/01-getting-started.md#-常见问题)。

---

## 📌 许可证

本项目采用 [MIT License](LICENSE)。
