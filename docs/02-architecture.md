# 🏗️ 02 - 系统架构

本章说明 XiaoQing Core 的运行时组件、所有权、生命周期和主要数据流。公开插件接口见 [API 参考](05-api-reference.md)，逐模块索引见 [Core 模块](04-core-modules.md)。

---

## 🏗️ 架构总览

```text
                           ┌────────────────────┐
                           │ config + secrets   │
                           │ revision snapshot  │
                           └─────────┬──────────┘
                                     │ publish
┌──────────────┐  events   ┌─────────▼──────────┐  actions   ┌──────────────┐
│ OneBot v11   ├──────────►│    XiaoQingApp     ├───────────►│ OneBot v11   │
│ HTTP / WS    │           │   stable facade    │            │ HTTP / WS    │
└──────────────┘           └─────────┬──────────┘            └──────────────┘
                                     │
                    ┌────────────────┼────────────────┐
                    │                │                │
             ┌──────▼──────┐  ┌──────▼──────┐  ┌─────▼───────┐
             │ Dispatcher  │  │PluginManager│  │  Scheduler  │
             │ + Router    │  │ generations │  │ + fanout    │
             └──────┬──────┘  └──────┬──────┘  └─────┬───────┘
                    │                │                │
                    └────────────────┼────────────────┘
                                     │ scoped capabilities
                           ┌─────────▼──────────┐
                           │      Plugins       │
                           │ commands/services  │
                           │ sessions/web/jobs  │
                           └────────────────────┘
```

Core 采用单进程异步架构。插件是受信任 Python 扩展，共享进程和系统权限。插件执行 gate、代际发布和能力作用域负责可用性、生命周期与接口组织。

---

## 🧩 应用门面与内部服务

`XiaoQingApp` 是进程级稳定门面。`core/app.py` 负责组件装配和少量公开入口，内部服务按职责分布在 `app_*` 模块：

| 模块 | 职责 |
|---|---|
| `app_config_apply.py` | 配置与敏感快照的串行发布 |
| `app_plugin_context.py` | 插件 Context、capability 和服务视图签发 |
| `app_lifecycle.py` | 启动事务、失败回滚和逆序资源回收 |
| `app_ingress.py` | Inbound Listener 的候选绑定与代际切换 |
| `app_delivery.py` | OneBot Action 投递、回执和事件收集 |
| `app_identity.py` | 管理员状态和 principal authority |
| `app_scheduling.py` | 插件排程发布与撤销 |
| `app_plugin_watch.py` | 插件文件 watcher 监督与重启 |

这些模块共享同一 `XiaoQingApp` 实例和进程生命周期。职责拆分用于单元测试、所有权追踪和局部维护。

---

## ⚙️ 配置发布

`ConfigManager` 读取 `config.json` 与 `secrets.json`，并生成带 revision 的只读快照。应用层按单一提交顺序发布以下状态：

1. 校验公开配置与敏感来源状态。
2. 解析管理员、Inbound token 和 OneBot token。
3. 更新插件配置视图与 AI route。
4. 重协商 Inbound 和主动 WebSocket 连接。
5. 发布调度与插件相关运行参数。

普通配置使用 last-known-good 快照；配置 watcher 会发布通过校验的公开配置来源。敏感配置使用 fail-closed 快照：删除、损坏、不可读或尚未与当前公开配置确认配对的 secrets 会立即撤销网络凭据与管理员视图。`ConfigManager` 管理的 secret 事务在持锁写入后直接发布已确认 revision。部署工具整体替换来源文件时，停服后的下一次启动负责确认新来源；保留独立可信 Inbound 的运行实例也可由管理员执行 `/reload` 完成确认与插件后台重载。

---

## 🌐 网络接入与发送

### Inbound Server

`server.py` 提供：

- `POST /event`
- `WebSocket /ws`
- `GET /health`
- `GET /metrics`

HTTP 与 WebSocket Inbound 共享鉴权策略、会话键、接纳队列和排序规则。配置切换先创建候选 Listener，完成验证后提交新代，再排空旧代。

### 主动 WebSocket Client

`onebot.py` 的 WebSocket Client 主动连接 OneBot，接收事件并发送 Action。连接管理包含有界指数退避、抖动、稳定窗口和配置变更唤醒。

### 出站发送

插件回复与主动消息统一进入应用投递层：

```text
插件消息段
  → Delivery
  → WebSocket Action 或 HTTP Action
  → 发送结果 / 回执
```

发送回执支持插件在成功投递后提交业务状态。图片和文件在发送前经过路径、大小、类型和读取预算校验。

---

## 💬 消息分发

`Dispatcher` 将 OneBot 事件转换为 `MessageContext`，并执行固定 A–G 流程：

1. 处理门控
2. URL 解析
3. Bot 名称回应
4. 活跃会话
5. 命令
6. 命令兜底提示
7. Smalltalk Provider

`Router` 使用 Manifest 构建的只读命令目录完成触发词匹配、子命令解析、权限校验和场景校验。启动阶段会检测同优先级触发词冲突并报告错误。

[消息处理流程](08-message-flow.md) 说明每个阶段的输入、短路条件和观测点。

---

## 🧩 插件运行时

### 发现与加载

`PluginManager` 按插件目录名称排序发现插件，依次完成：

1. 读取并校验 `plugin.json`。
2. 校验插件名称、入口路径、依赖和命令冲突。
3. 构建源码与资源快照。
4. 导入独立代际命名空间。
5. 创建插件作用域 Context。
6. 执行 `init()`。
7. 原子发布命令、服务和调度入口。

### 代际发布

每次加载或热重载产生一个插件代。候选代完成校验与初始化后提交为活动代，退役代进入请求排空。资源按创建顺序逆序回收。

### 文件监视

插件 watcher 对 Python 源码、插件根 JSON 和 Manifest `watch_files` 建立稳定快照。当前解释器通过 module-lock 能力探针后可执行热重载；restart-only 模式通过进程重启应用源码变化。

### 执行治理

每个插件拥有独立入口并发、排队、超时、熔断和同步任务预算。同步阻塞库通过 `run_sync()` 进入共享 worker 与按插件 bulkhead。全局调度器在插件之间公平选择任务。

Manifest `concurrency` 控制命令入口模式：

- `parallel`：插件入口可按配置并行。
- `sequential`：插件入口按插件串行。

---

## 🧩 插件 Context 与能力

Core 为每个插件签发窄 Context。主要能力包括：

- 当前插件的公开配置与 secret 快照
- 私有数据目录与插件代内 runtime 状态
- 日志、指标和 HTTP Session
- Session API
- 主动发送与回执
- AI route
- 声明式服务调用
- 调度与静音查询

Context 通过 capability 对象表达权限。敏感状态保留在 Core 所有权内，插件获得完成自身任务所需的作用域视图。

---

## 💬 会话管理

`SessionManager` 使用会话键区分交互：

- 私聊：用户 ID
- 群聊：群 ID + 用户 ID

同一会话键按接纳顺序串行执行。处理函数读取会话快照，并通过原子 callback 提交状态。异常与取消会保留已提交状态的一致性。

Session 适合用户驱动的短交互。Codex 等长任务使用插件内部队列和主动发送能力。

---

## 💾 调度与持久投递

`SchedulerManager` 从 Manifest `schedule` 读取 cron 任务，按插件代注册 handler。`durable_fanout.py` 为多目标通知记录持久进度，进程重启后从检查点继续。

插件排程发布与插件代绑定。卸载或重载时，旧代排程先停止触发，再进入资源排空。

---

## 🧩 插件内嵌服务

复合插件可在 `init()` 中创建内部服务，并在 `shutdown()` 中关闭资源。Pendo 使用该模式管理 FastAPI、SQLite、前端资源、提醒调度和聊天命令。

内嵌服务遵循以下所有权：

- 插件生命周期拥有服务启动与停止。
- 插件数据目录拥有数据库与持久文件。
- Core Context 提供日志、配置、密钥和发送能力。
- 服务内部按数据库、认证、API、任务和前端资源划分模块。

---

## 🚀 启动与关闭顺序

### 启动

1. 读取并校验配置快照。
2. 初始化日志、HTTP、指标与会话服务。
3. 加载插件并发布 Context、服务和命令目录。
4. 发布插件排程。
5. 启动 Inbound 与主动 WebSocket。
6. 启动配置和插件 watcher。

### 关闭

1. 停止新事件接纳。
2. 停止 watcher 与排程触发。
3. 排空插件入口和后台资源。
4. 逆序关闭插件、网络连接和共享服务。
5. 刷新持久状态与日志。

---

## 🏗️ 数据所有权

| 数据 | 所有者 | 默认位置 |
|---|---|---|
| 公开运行配置 | ConfigManager | `config/config.json` |
| 敏感配置 | ConfigManager | `config/secrets.json` |
| 插件源码 | PluginManager | `plugins/<name>/` |
| 插件运行数据 | 对应插件 | `data/<name>/` |
| 多轮会话 | SessionManager | 进程内状态 |
| 运行日志 | 日志系统 | `logs/` |
| UAT 报告 | UAT Runner | `test_reports/` |

明确的数据所有权使代码同步、生产数据保留、备份和恢复可以独立执行。

---

## 🧭 下一步

- 逐模块职责：[Core 模块](04-core-modules.md)
- 事件阶段：[消息处理流程](08-message-flow.md)
- 插件契约：[插件开发指南](03-plugin-development.md)
