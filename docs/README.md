# 📚 XiaoQingBot 文档目录

本目录是 XiaoQingBot 的项目手册。文档按“认识项目 → 启动服务 → 理解架构 → 开发插件 → 查询接口与配置 → 深入运行机制 → 使用插件”的顺序组织。

---

## 📚 主题索引

| 文档 | 主要职责 | 目标读者 |
|---|---|---|
| [00 - 项目概览](00-overview.md) | 项目定位、能力边界、核心概念和目录结构 | 所有读者 |
| [01 - 快速开始](01-getting-started.md) | 安装、配置、OneBot 接入、启动和首次验证 | 新用户、部署者 |
| [02 - 系统架构](02-architecture.md) | 运行时组件、服务边界、生命周期和数据流 | 框架维护者 |
| [03 - 插件开发](03-plugin-development.md) | Manifest、入口、上下文、会话、调度和测试 | 插件开发者 |
| [04 - Core 模块](04-core-modules.md) | `core/` 模块职责与协作关系 | 框架维护者 |
| [05 - API 参考](05-api-reference.md) | PluginContext、消息段、会话和插件入口签名 | 插件开发者 |
| [06 - 配置详解](06-configuration.md) | 配置字段、secrets、AI 路由、网络安全和热重载 | 部署者、管理员 |
| [07 - 高级主题](07-advanced.md) | 后台任务、调度、服务能力、性能和部署模式 | 高级开发者 |
| [08 - 消息流程](08-message-flow.md) | OneBot 事件从接收到回复的完整路径 | 排障人员、框架维护者 |
| [09 - 插件目录](09-plugins.md) | 内置插件的用途、入口命令和专项文档 | 用户、管理员 |
| [Pendo Scriptable 小组件](pendo-scriptable-widget.md) | iPhone 小组件鉴权、脚本和设置步骤 | Pendo 用户 |
| [更新记录](../CHANGELOG.md) | 各版本的功能变化、影响范围和验证结果 | 用户、维护者 |

---

## 📌 推荐阅读路线

### 第一次运行

1. 阅读 [项目概览](00-overview.md)，了解 OneBot、Core、插件和数据目录。
2. 按 [快速开始](01-getting-started.md) 完成安装、配置、接入和启动。
3. 通过 [配置详解](06-configuration.md) 完成生产参数与 secrets 设置。
4. 在 [插件目录](09-plugins.md) 选择功能，并进入对应插件 README。

### 开发插件

1. 按 [插件开发](03-plugin-development.md) 创建 Manifest 和入口。
2. 通过 [API 参考](05-api-reference.md) 查询公开接口。
3. 通过 [高级主题](07-advanced.md) 接入后台任务、调度和声明式服务。
4. 通过 [Core 模块](04-core-modules.md) 理解框架实现边界。

### 排查消息链路

1. 通过 [消息流程](08-message-flow.md) 定位当前处理阶段。
2. 通过 [系统架构](02-architecture.md) 找到负责该阶段的组件。
3. 通过 [配置详解](06-configuration.md) 核对连接、权限、路由和执行限制。
4. 通过插件 README 核对插件自己的触发条件与依赖。

---

## 🧩 核心插件文档

| 插件 | 使用说明 | 架构说明 |
|---|---|---|
| `xiaoqing_chat` | [README](../plugins/xiaoqing_chat/README.md) | [ARCHITECTURE](../plugins/xiaoqing_chat/ARCHITECTURE.md) |
| `pendo` | [README](../plugins/pendo/README.md) | [ARCHITECTURE](../plugins/pendo/ARCHITECTURE.md) |

其他插件均在各自目录提供 `README.md`；[插件目录](09-plugins.md) 提供统一入口。

---

## 🏗️ 文档职责

- 根目录 README 提供项目入口和最短启动路径。
- `docs/00-09` 分别维护一个明确主题。
- 插件 README 面向插件用户和部署者。
- 插件 ARCHITECTURE 面向插件维护者。
- 命令 Manifest 是运行时帮助、权限、场景和样例的权威来源。
- Changelog 记录版本变化与验证结果。

文档中的命令、字段、默认值和路径以代码、配置示例、Manifest 和测试契约为依据。

---

## 🧭 外部参考

- [OneBot 协议](https://onebot.dev/)
- [NapCatQQ](https://github.com/NapNeko/NapCatQQ)
- [APScheduler](https://apscheduler.readthedocs.io/)
- [FastAPI](https://fastapi.tiangolo.com/)
- [aiohttp](https://docs.aiohttp.org/)
- [Python asyncio](https://docs.python.org/3/library/asyncio.html)
