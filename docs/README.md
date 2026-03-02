# XiaoQing 开发文档

欢迎阅读 XiaoQing 开发文档！本文档将帮助你全面理解 XiaoQing 框架并进行开发。

## 📚 文档目录

### 入门篇

| 文档 | 内容 | 适合人群 |
|------|------|----------|
| [00-overview.md](00-overview.md) | 项目概览、设计理念、核心概念 | 所有人 |
| [01-getting-started.md](01-getting-started.md) | 安装配置、快速启动、连接 NapCatQQ | 新手入门 |

### 架构篇

| 文档 | 内容 | 适合人群 |
|------|------|----------|
| [02-architecture.md](02-architecture.md) | 系统架构、数据流、模块职责 | 想深入了解的开发者 |
| [04-core-modules.md](04-core-modules.md) | 核心模块源码详解 | 框架开发者 |
| [08-message-flow.md](08-message-flow.md) | 消息处理流程与并发控制详解 | 框架开发者/运维 |

### 开发篇

| 文档 | 内容 | 适合人群 |
|------|------|----------|
| [03-plugin-development.md](03-plugin-development.md) | 插件开发完整指南、最佳实践 | 插件开发者 |
| [07-advanced.md](07-advanced.md) | 高级主题：多轮对话、定时任务、扩展 | 高级开发者 |
| [09-plugins.md](09-plugins.md) | 29 个内置插件功能说明 | 所有人 |

### 参考篇

| 文档 | 内容 | 适合人群 |
|------|------|----------|
| [05-api-reference.md](05-api-reference.md) | 完整 API 参考手册 | 所有开发者 |
| [06-configuration.md](06-configuration.md) | 配置项详解、最佳实践 | 运维/部署 |

---

## 🚀 快速导航

### 我是新手，想快速上手
1. 先看 [00-overview.md](00-overview.md) 了解这是什么
2. 跟着 [01-getting-started.md](01-getting-started.md) 运行起来
3. 阅读 [03-plugin-development.md](03-plugin-development.md) 写第一个插件

### 我想开发插件
1. [03-plugin-development.md](03-plugin-development.md) - 插件开发完整指南
2. [05-api-reference.md](05-api-reference.md) - API 参考
3. [07-advanced.md](07-advanced.md) - 多轮对话、定时任务等高级功能
4. [09-plugins.md](09-plugins.md) - 参考内置插件的实现

### 我想深入理解框架
1. [02-architecture.md](02-architecture.md) - 系统架构
2. [04-core-modules.md](04-core-modules.md) - 核心模块源码
3. [08-message-flow.md](08-message-flow.md) - 消息处理与并发控制
4. 直接阅读 `core/` 目录下的源代码

### 我想部署/运维
1. [01-getting-started.md](01-getting-started.md) - 安装和启动
2. [06-configuration.md](06-configuration.md) - 配置详解（含并发控制参数）
3. [08-message-flow.md](08-message-flow.md) - 消息队列与并发控制机制

---

## 📖 阅读建议

- **通篇阅读**: 约 2-3 小时可完整阅读所有文档
- **快速上手**: 30 分钟阅读 00、01、03 即可开始开发
- **按需查阅**: 开发过程中遇到问题时查阅对应章节

## 🔗 相关链接

- [OneBot 协议文档](https://onebot.dev/)
- [NapCatQQ](https://github.com/NapNeko/NapCatQQ) - 推荐的 OneBot 实现
- [APScheduler 文档](https://apscheduler.readthedocs.io/)
- [aiohttp 文档](https://docs.aiohttp.org/)
- [Python asyncio 官方文档](https://docs.python.org/3/library/asyncio.html)

---

> 如果文档有任何问题或建议，欢迎提交 Issue 或 PR！
