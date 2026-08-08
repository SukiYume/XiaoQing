# 🧭 00 - 项目概览

本章介绍 XiaoQingBot 的定位、能力范围、核心概念和项目目录。完成阅读后，可进入 [快速开始](01-getting-started.md) 部署服务。

---

## ✨ 项目定位

XiaoQingBot 是基于 Python asyncio 和 OneBot v11 的插件化 QQ 助手。项目同时包含两部分：

- `core/`：负责 OneBot 接入、事件解析、命令路由、会话、调度、配置、插件生命周期、执行治理和运行指标。
- `plugins/`：提供聊天、个人管理、科研工具、远程操作、内容解析、外部服务和娱乐功能。

OneBot 实现负责 QQ 客户端连接，XiaoQing 负责事件处理与业务能力：

```text
QQ ↔ OneBot 实现 ↔ XiaoQing Core ↔ 插件
```

项目适合运行在个人电脑、家庭服务器、NAS、可信内网服务或受反向代理保护的私人服务器。

---

## ✨ 能力版图

| 领域 | 代表插件 | 能力 |
|---|---|---|
| 智能聊天 | `xiaoqing_chat`, `smalltalk`, `chat`, `voice` | 多模态聊天、群聊参与、基础问答、TTS |
| 个人管理 | `pendo` | 日程、待办、笔记、日记、账本、提醒、Web、小组件 |
| 天文科研 | `apod`, `arxiv_filter`, `ads_paper`, `astro_tools`, `dict`, `chime` | 天文图、论文筛选、文献检索、计算、词典、FRB 数据 |
| 执行与运维 | `codex`, `shell`, `qingssh`, `jupyter`, `minecraft` | 后台代理、受控终端、SSH、Python REPL、RCON |
| 内容与服务 | `url_parser`, `github`, `earthquake`, `twitter`, `signin`, `adnmb` | 链接解析、趋势、快讯、媒体提取、签到、社区浏览 |
| 工具与娱乐 | `choice`, `color`, `wolframalpha`, `guess_number`, `qingpet`, `echo` | 随机选择、颜色、计算、游戏、宠物、回显 |

[插件目录](09-plugins.md) 提供全部插件的入口命令和专项文档。

---

## ✨ 核心概念

### OneBot

[OneBot v11](https://onebot.dev/) 定义 QQ 事件、消息段、Action 和通信协议。XiaoQing 支持以下接入方向：

- 被动 Inbound：OneBot 通过 HTTP `/event` 或 WebSocket `/ws` 推送事件。
- 主动 WebSocket：XiaoQing 连接 OneBot 的 WebSocket 服务。
- 出站发送：XiaoQing 通过 HTTP Action 或 WebSocket Action 发送回复。

### Core

Core 将网络事件转换为统一消息上下文，依次执行处理门控、URL 解析、名字回应、会话、命令和闲聊流程。Core 还负责插件加载、配置快照、定时任务、主动投递和资源回收。

### 插件

插件目录包含 `plugin.json` 和 Python 入口。Manifest 声明插件元数据、命令树、权限、使用场景、调度任务、依赖和文件监视范围。Core 按确定顺序加载插件，并在启动阶段校验命令冲突和依赖声明。

插件采用受信任代码模型，与 Bot 共享 Python 进程和操作系统权限。插件作用域 Context 提供当前插件的配置、secret、数据目录、HTTP、AI、会话、指标和发送能力。

### 命令目录

Manifest 中的命令树是运行时命令目录的权威来源。Core 由同一目录生成：

- 文本 `/help`
- JSON 命令目录
- 路由规则
- 权限与场景校验
- 示例和错误示例测试

帮助系统支持逐层浏览：

```text
/help
/help pendo
/help pendo todo
/help pendo todo add
```

### 消息段

OneBot 消息由有序消息段组成。常见类型包括 `text`、`image`、`record`、`face`、`mface` 和 `reply`。插件通过 `core.plugin_base` 构造文本、图片、语音和组合回复。

### 会话与后台任务

框架 Session 适合需要用户连续输入的短期交互，例如表单、猜数字、SSH 和 Jupyter REPL。Session 按会话键串行处理，并通过快照事务提交状态。

插件后台队列适合 Codex 等长任务。任务独立运行，完成后通过主动投递能力回传结果。

### 配置与数据

- `config/config.json` 保存公开运行设置。
- `config/secrets.json` 保存管理员身份、token 和第三方密钥。
- `data/<plugin_name>/` 保存插件运行数据。
- `logs/` 保存运行日志。

配置、源码和运行数据采用独立目录，便于部署、备份和恢复。

---

## 🔄 运行路径

一条消息的高层路径如下：

```text
OneBot 事件
  → Inbound Server 或主动 WebSocket Client
  → XiaoQingApp
  → Dispatcher
  → Router / Session / Smalltalk Provider
  → 插件入口
  → OneBot 消息段
  → HTTP 或 WebSocket Action
```

完整阶段与短路条件见 [消息处理流程](08-message-flow.md)，组件所有权见 [系统架构](02-architecture.md)。

---

## 💾 项目目录

```text
XiaoQing/
├── main.py                  # 进程入口
├── pyproject.toml           # 项目元数据与工具配置
├── requirements.txt         # 直接依赖
├── config/                  # 配置示例与本机配置
├── core/                    # Core 运行时
├── plugins/                 # 插件源码
├── data/                    # 插件运行数据
├── logs/                    # 运行日志
├── docs/                    # 项目手册
├── scripts/                 # 启动、UAT、同步和维护工具
├── tests/                   # 自动化测试
└── test_reports/            # 本地 UAT 报告
```

Core 的内部职责进一步分布在以下模块组：

| 模块组 | 主要职责 |
|---|---|
| `app.py`, `app_*` | 应用门面、配置发布、能力签发、网络协商、调度和生命周期 |
| `dispatcher.py`, `router.py` | 消息分发和命令解析 |
| `plugin_manager.py`, `plugin_*` | 插件发现、代际发布、执行治理和文件监视 |
| `session.py`, `scheduler.py` | 多轮会话和定时任务 |
| `onebot.py`, `server.py` | OneBot 出站通信和 Inbound 接入 |
| `context.py`, `interfaces.py`, `plugin_base.py` | 插件公开能力和工具函数 |
| `config.py`, `ai.py`, `metrics.py` | 配置、模型路由和运行指标 |

[Core 模块详解](04-core-modules.md) 提供逐模块索引。

---

## 🔐 设计边界

### 受信任插件

插件与 Bot 共享进程和系统权限。部署者负责插件代码审查与来源管理。Core 的命名空间、执行 gate 和代际管理负责能力组织、可用性与生命周期一致性。

### 有界资源

网络请求、插件入口、同步任务、队列、缓存和关闭流程均采用明确预算。过载请求进入快速错误路径，资源回收按所有权逆序执行。

### 单一事实来源

- Manifest 定义命令和插件元数据。
- 配置示例定义公开配置结构。
- `PluginContextProtocol` 定义插件公开上下文。
- 插件数据目录定义运行状态所有权。
- 测试契约验证文档中的关键接口。

### 平台中立启动

项目通过当前环境中的 `python main.py` 启动。依赖与 Python 环境由使用者准备，项目脚本直接调用 `PATH` 中的工具。

---

## 🧭 下一步

继续阅读 [快速开始](01-getting-started.md)，完成安装、配置和首次启动。
