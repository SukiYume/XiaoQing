# XiaoQingBot 文档目录

`docs/` 是 XiaoQingBot 的项目手册。00-09 文档按阅读顺序组织，先帮读者跑起来，再逐步展开项目概览、安装部署、架构、插件开发、核心模块、API、配置、高级用法、消息链路和内置插件清单。

只想先部署运行时，主要阅读 `00`、`01`、`06` 和 `09`。框架维护和插件开发需要阅读 `00-09`。重点维护 `xiaoqing_chat` 或 `pendo` 时，还需要阅读对应插件目录下的 `README.md` 和 `ARCHITECTURE.md`；配置 Codex、arXiv 摘要和 shell 这类工具链时，优先看 `06` 和 `09`。

## 文档范围

| 文档 | 主题 | 主要内容 | 适合读者 |
|---|---|---|---|
| [00-overview.md](00-overview.md) | 项目概览 | 项目定位、核心概念、运行方式、目录结构、设计原则 | 所有人 |
| [01-getting-started.md](01-getting-started.md) | 快速开始 | 环境、依赖、配置、OneBot 接入、启动、基础验证 | 首次部署者 |
| [02-architecture.md](02-architecture.md) | 系统架构 | App、Dispatcher、Router、PluginManager、Session、Scheduler、OneBot、Web 插件 | 框架维护者 |
| [03-plugin-development.md](03-plugin-development.md) | 插件开发 | 插件结构、`plugin.json`、`handle()`、session、smalltalk、消息段、生命周期、错误处理 | 插件开发者 |
| [04-core-modules.md](04-core-modules.md) | 核心模块 | `core/` 各模块职责、关键类、处理链、配置、通信和工具函数 | 深入维护者 |
| [05-api-reference.md](05-api-reference.md) | API 参考 | `PluginContext`、消息段构造、session、handler 返回值、smalltalk provider 接口 | 插件开发者 |
| [06-configuration.md](06-configuration.md) | 配置详解 | `config.json`、`secrets.json`、OneBot、插件配置、热重载、安全和部署 | 部署/运维 |
| [07-advanced.md](07-advanced.md) | 高级用法 | 多轮会话、调度、权限、Web 插件、复杂 smalltalk、性能和调试 | 高级开发者 |
| [08-message-flow.md](08-message-flow.md) | 消息流程 | OneBot 事件、解析、线性分发、命令、会话、闲聊、发送 | 排障和框架维护 |
| [09-plugins.md](09-plugins.md) | 内置插件 | 所有内置插件的功能、命令、配置、示例和注意事项 | 使用者和维护者 |

补充文档如下。

| 文档 | 内容 |
|---|---|
| [../CHANGELOG.md](../CHANGELOG.md) | 项目更新记录，按日期整理每次维护内容 |
| [pendo-scriptable-widget.md](pendo-scriptable-widget.md) | Pendo iPhone Scriptable 小组件配置和只读 API |

## 核心插件文档

| 插件 | 使用手册 | 架构文档 | 说明 |
|---|---|---|---|
| `xiaoqing_chat` | [plugins/xiaoqing_chat/README.md](../plugins/xiaoqing_chat/README.md) | [plugins/xiaoqing_chat/ARCHITECTURE.md](../plugins/xiaoqing_chat/ARCHITECTURE.md) | 多模态拟人聊天、群聊参与、记忆、规划、reply checker |
| `pendo` | [plugins/pendo/README.md](../plugins/pendo/README.md) | [plugins/pendo/ARCHITECTURE.md](../plugins/pendo/ARCHITECTURE.md) | 日程、待办、笔记、日记、账本、提醒、Web 控制台 |

## 按目标阅读

### 第一次运行 XiaoQingBot

1. [00-overview.md](00-overview.md): 先理解 OneBot、插件、命令、消息段和上下文。
2. [01-getting-started.md](01-getting-started.md): 创建配置，连接 NapCatQQ，启动进程。
3. [06-configuration.md](06-configuration.md): 检查 `config.json`、`secrets.json` 和插件配置。
4. [09-plugins.md](09-plugins.md): 找到要启用的插件和命令。

### 配置 xiaoqing_chat

1. [09-plugins.md](09-plugins.md) 中的 `xiaoqing_chat` 章节。
2. [plugins/xiaoqing_chat/README.md](../plugins/xiaoqing_chat/README.md): 配置 provider、attention gate、多模态和测试命令。
3. [plugins/xiaoqing_chat/ARCHITECTURE.md](../plugins/xiaoqing_chat/ARCHITECTURE.md): 理解主链路、planner、memory、media 和 reply checker。
4. [08-message-flow.md](08-message-flow.md): 理解 `smalltalk_provider = xiaoqing_chat` 时框架如何把消息交给插件。

### 配置 Pendo

1. [09-plugins.md](09-plugins.md) 中的 `pendo` 章节。
2. [plugins/pendo/README.md](../plugins/pendo/README.md): 学习聊天命令、Web 控制台、迁移、备份和排障。
3. [plugins/pendo/ARCHITECTURE.md](../plugins/pendo/ARCHITECTURE.md): 理解数据模型、SQLite、Web API、调度和 Bundle。
4. [pendo-scriptable-widget.md](pendo-scriptable-widget.md): 配置 iPhone 主屏小组件。

### 配置 Codex、arXiv 摘要和 Shell 工具

1. [06-configuration.md](06-configuration.md): 配置 `plugins.codex` 的默认工作目录、允许目录、并发数、sandbox、`astro-ph` 摘要会话和方法论文件名；配置 shell 白名单。
2. [09-plugins.md](09-plugins.md): 查看 `/codex create`、`/codex <name> <任务>`、`/codex cancel`、`/arxiv`、`/shell` 等命令示例。
3. [08-message-flow.md](08-message-flow.md): 理解普通 bot session 与 Codex 独立后台队列的区别。

### 开发新插件

1. [03-plugin-development.md](03-plugin-development.md): 从插件目录结构和 `plugin.json` 开始。
2. [05-api-reference.md](05-api-reference.md): 查 `PluginContext`、消息段、session 和返回值。
3. [07-advanced.md](07-advanced.md): 学定时任务、多轮会话、权限控制、Web 服务和 smalltalk provider。
4. [04-core-modules.md](04-core-modules.md): 需要改框架时再深入 core 模块。

### 排查消息为什么没回复

1. [08-message-flow.md](08-message-flow.md): 阅读消息处理链。
2. [02-architecture.md](02-architecture.md): 确认 Dispatcher、Router、Session、Smalltalk 的职责。
3. [06-configuration.md](06-configuration.md): 检查 bot name、命令前缀、静音、OneBot 地址、smalltalk provider。
4. 对 `xiaoqing_chat`，继续看 [plugins/xiaoqing_chat/README.md](../plugins/xiaoqing_chat/README.md) 的 attention gate 和 participation gate。

## 文档维护约定

- 00-09 是长期项目手册，描述“当前项目如何工作”，不要写成变更流水账。
- 项目更新记录写入根目录 [CHANGELOG.md](../CHANGELOG.md)，不要混入 00-09 手册。
- 插件自己的 README 面向使用者，ARCHITECTURE 面向维护者。
- 测试 prompt、运行报告、历史设计草案不作为当前行为的权威入口。
- 配置示例优先和 `config/*.example`、`plugin.json`、源码实现保持一致。
- 如果文档和代码不一致，维护时先以代码和测试为准，再修正文档。

## 外部参考

- [OneBot 协议文档](https://onebot.dev/)
- [NapCatQQ](https://github.com/NapNeko/NapCatQQ)
- [APScheduler 文档](https://apscheduler.readthedocs.io/)
- [FastAPI 文档](https://fastapi.tiangolo.com/)
- [aiohttp 文档](https://docs.aiohttp.org/)
- [Python asyncio 官方文档](https://docs.python.org/3/library/asyncio.html)
