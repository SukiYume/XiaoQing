# 📚 XiaoQing 开发文档

这组文档覆盖三类问题：

- 我怎样把 XiaoQing 跑起来
- 我怎样理解框架和消息链路
- 我怎样基于现有插件继续开发

---

## 🚀 推荐入口

| 目标 | 从这里开始 | 说明 |
|------|------------|------|
| 第一次接触项目 | [00-overview.md](00-overview.md) | 先建立整体认知，再进入安装和源码阅读 |
| 想尽快跑起来 | [01-getting-started.md](01-getting-started.md) | 安装、配置、连接 OneBot、基础验证 |
| 想开发插件 | [03-plugin-development.md](03-plugin-development.md) | 插件结构、生命周期、最佳实践 |
| 想排查部署/配置问题 | [06-configuration.md](06-configuration.md) | 配置项、示例、部署注意事项 |
| 想看内置能力 | [09-plugins.md](09-plugins.md) | 可直接加载的内置插件清单、命令和配置说明 |

---

## 🗂️ 文档地图

### 🌱 入门

| 文档 | 内容 | 适合人群 |
|------|------|----------|
| [00-overview.md](00-overview.md) | 项目概览、设计理念、核心概念、目录结构 | 所有人 |
| [01-getting-started.md](01-getting-started.md) | 安装、配置、启动、联调 OneBot | 新手 / 首次部署 |

### 🏗️ 架构

| 文档 | 内容 | 适合人群 |
|------|------|----------|
| [02-architecture.md](02-architecture.md) | 系统架构、组件关系、职责边界 | 想理解框架整体设计的开发者 |
| [04-core-modules.md](04-core-modules.md) | `core/` 模块源码说明 | 想深入理解 core 源码的开发者 |
| [08-message-flow.md](08-message-flow.md) | 消息从接收到回复的完整处理链路 | 框架开发者 / 运维 |

### 💻 开发

| 文档 | 内容 | 适合人群 |
|------|------|----------|
| [03-plugin-development.md](03-plugin-development.md) | 插件开发完整指南 | 插件开发者 |
| [07-advanced.md](07-advanced.md) | 多轮对话、定时任务、扩展技巧 | 需要定制复杂行为的开发者 |
| [09-plugins.md](09-plugins.md) | 内置插件清单与命令示例 | 想复用现有能力的人 |

### 📖 参考

| 文档 | 内容 | 适合人群 |
|------|------|----------|
| [05-api-reference.md](05-api-reference.md) | 常用 API 和上下文接口参考 | 所有开发者 |
| [06-configuration.md](06-configuration.md) | 配置项详解、最佳实践、部署说明 | 运维 / 部署 / 调参 |

---

## 🧭 按角色阅读

### 我是新手

1. [00-overview.md](00-overview.md)
2. [01-getting-started.md](01-getting-started.md)
3. [03-plugin-development.md](03-plugin-development.md)

### 我想开发插件

1. [03-plugin-development.md](03-plugin-development.md)
2. [05-api-reference.md](05-api-reference.md)
3. [07-advanced.md](07-advanced.md)
4. [09-plugins.md](09-plugins.md)

### 我想理解框架内部

1. [02-architecture.md](02-architecture.md)
2. [04-core-modules.md](04-core-modules.md)
3. [08-message-flow.md](08-message-flow.md)
4. 结合 `core/` 与 `tests/` 源码交叉阅读

### 我想部署和排障

1. [01-getting-started.md](01-getting-started.md)
2. [06-configuration.md](06-configuration.md)
3. [08-message-flow.md](08-message-flow.md)

---

## ⏱️ 阅读建议

| 目标 | 推荐文档 | 预计时间 |
|------|----------|----------|
| 快速跑通 | `00` + `01` | ≈ 20 分钟 |
| 开始二次开发 | `00` + `03` + `05` | ≈ 45 分钟 |
| 系统级理解 | `02` + `04` + `08` | ≈ 1.5 小时 |
| 按需查阅 | 对应章节 | 视问题而定 |

---

## 🔗 外部链接

- [OneBot 协议文档](https://onebot.dev/)
- [NapCatQQ](https://github.com/NapNeko/NapCatQQ)
- [APScheduler 文档](https://apscheduler.readthedocs.io/)
- [aiohttp 文档](https://docs.aiohttp.org/)
- [Python asyncio 官方文档](https://docs.python.org/3/library/asyncio.html)

---

> 文档内容会随插件和配置演进而更新。发现示例与代码不一致时，请优先以仓库中的 `plugin.json`、`config/*.example` 和源码实现为准。

> `docs/plans/` 用于本地规划、review 和临时笔记，默认被 `.gitignore` 忽略，不属于版本化文档的一部分。
