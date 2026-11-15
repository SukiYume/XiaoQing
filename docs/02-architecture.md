# 🏗️ 02 - 系统架构

中文 | [English](en/02-architecture.md)

本章说明 XiaoQing Core 的运行时组件、所有权、生命周期和主要数据流，以及小青四个基础插件之间的分工。
公开插件接口见 [API 参考](05-api-reference.md)，逐模块索引见 [Core 模块](04-core-modules.md)。

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

Core 采用单进程异步架构。
插件是受信任 Python 扩展，共享进程和系统权限。
插件执行 gate、代际发布和能力作用域负责可用性、生命周期与接口组织。

---

## 🧩 应用门面与内部服务

`XiaoQingApp` 是进程级稳定门面。
`core/app.py` 负责组件装配和少量公开入口，内部服务按职责分布在 `app_*` 模块：

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

这些模块共享同一 `XiaoQingApp` 实例和进程生命周期。
职责拆分用于单元测试、所有权追踪和局部维护。

---

## ⚙️ 配置发布

`ConfigManager` 读取 `config.json` 与 `secrets.json`，并生成带 revision 的只读快照。
应用层按单一提交顺序发布以下状态：

1. 校验公开配置与敏感来源状态。
2. 解析管理员、Inbound token 和 OneBot token。
3. 更新插件配置视图与 AI route。
4. 重协商 Inbound 和主动 WebSocket 连接。
5. 发布调度与插件相关运行参数。

普通配置使用 last-known-good 快照。
配置 watcher 会发布通过校验的公开配置来源。
敏感配置使用 fail-closed 快照：删除、损坏、不可读或与新公开配置尚未确认配对的 secrets 会立即撤销网络凭据与管理员视图。
`ConfigManager` 管理的 secret 事务在持锁写入后直接发布已确认 revision。
公开配置与当前已确认签名一致且运行代可信时，完整有效的外部 secrets 单文件变更进入待确认候选，已确认快照继续服务，候选隔离于插件、管理员和网络认证视图。
Core 仅把字段路径差异私聊给当前管理员，随后由 `/reload` 重新读取磁盘并原子发布。
公开配置变更与双来源部署由停服启动流程或独立可信 Inbound 完成确认。

---

## 🌐 网络接入与发送

### Inbound Server

`server.py` 提供：

- `POST /event`
- `WebSocket /ws`
- `GET /health`
- `GET /metrics`

HTTP 与 WebSocket Inbound 共享鉴权策略、会话键、接纳队列和排序规则。
两个入口还共用一份排队消息登记（`inbound_backlog.py`）：已到达、还没开始处理的消息按发送者记下，经 `inbound_backlog` capability 提供给插件查询。
配置切换先创建候选 Listener，完成验证后提交新代，再排空旧代。

标准 HTTP 上报的回复进入 OneBot action API，并在实际确认后返回 `{}`。
开发客户端可显式选择 `X-XiaoQing-Response-Mode: actions` 协议，接收待执行动作列表。
入站 Bearer token 与出站 OneBot token 分别管理。

### 主动 WebSocket Client

`onebot.py` 的 WebSocket Client 主动连接 OneBot，接收事件并发送 Action。
连接管理包含有界指数退避、抖动、稳定窗口和配置变更唤醒。
连接建立后的上线通知回调可能等待 Action 的 echo，因此回调与接收循环并发运行，echo 由接收循环处理。
连接关闭时统一取消和等待所属回调任务。

### 出站发送

插件回复与主动消息统一进入应用投递层：

```text
插件消息段
  → Delivery
  → WebSocket Action 或 HTTP Action
  → 发送结果 / 回执
```

发送回执支持插件在成功投递后提交业务状态。
长消息被拆成几条发出时，回执等全部物理消息确认后才提交，并按顺序收集每一条的消息 ID（`message_ids`），插件在提交回调里把业务记录和真实消息对应起来。
图片和文件在发送前经过路径、大小、类型和读取预算校验。

每个事件产生的回复先收集到事件暂存队列，由它负责这些动作的后续结算：收集成功时回执保持待定，动作实际投递后才提交或回滚。

---

## 💬 消息分发

`Dispatcher` 将 OneBot 事件转换为 `MessageContext`，并执行固定 A–G 流程：

- A. 处理门控
- B. URL 解析
- C. Bot 名称回应
- D. 活跃会话
- E. 命令
- F. 命令兜底提示
- G. Smalltalk Provider

`Router` 使用 Manifest 构建的只读命令目录完成触发词匹配和子命令解析。
启动阶段会检测同优先级触发词冲突并报告错误。

Dispatcher 在调用插件前用 `command_access` 校验权限与场景。
`command_access` 是全项目统一的权限判断函数：命令执行、会话继续前的复核、Core 派生能力和 agent 的模型可见命令清单都调用它。
插件代码将管理员检查交给 Core。

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

每次加载或热重载产生一个插件代。
候选代完成校验与初始化后提交为活动代，退役代进入请求排空。
资源按创建顺序逆序回收。

### 文件监视

插件 watcher 对 Python 源码、插件根 JSON 和 Manifest `watch_files` 建立稳定快照。
当前解释器通过 module-lock 能力探针后可执行热重载。
restart-only 模式通过进程重启应用源码变化。

### 执行治理

每个插件拥有独立入口并发、排队、超时、熔断和同步任务预算。
同步阻塞库通过 `run_sync()` 进入共享 worker 与按插件 bulkhead。
全局调度器在插件之间公平选择任务。

Manifest `concurrency` 控制命令入口模式：

- `parallel`：插件入口可按配置并行。
- `sequential`：插件入口按插件串行。

---

## 🧩 插件 Context 与能力

Core 为每个插件签发窄 Context。
主要能力包括：

- 当前插件的公开配置与 secret 快照
- 私有数据目录与插件代内 runtime 状态
- 日志、指标和 HTTP Session
- Session API
- 主动发送与回执
- AI route
- 声明式服务调用
- 调度与静音查询

Context 通过 capability 对象表达权限。
敏感状态保留在 Core 所有权内，插件获得完成自身任务所需的作用域视图。
派生能力跟随对应命令的 Manifest 声明签发，并在每次调用时按当前配置复核：密钥管理跟随 `/set_secret`，arXiv 的 Codex 摘要跟随 `/codex`，agent 的本机工具、SSH、Codex 跟随 `/shell`、`/ssh`、`/codex`。

---

## 💬 会话管理

`SessionManager` 使用会话键区分交互：

- 私聊：用户 ID
- 群聊：群 ID + 用户 ID

同一会话键按接纳顺序串行执行。
处理函数读取会话快照，并通过原子 callback 提交状态。
异常与取消会保留已提交状态的一致性。
每次继续前，Dispatcher 用 `command_access` 复核此人此刻在这里至少还能用该插件的一条顶层命令。
撤权或场景不再允许时会话关闭。

Session 适合用户驱动的短交互。
Codex、XiaoQing Agent 等长任务使用插件内部队列和主动发送能力。

---

## 💾 调度与持久投递

`SchedulerManager` 从 Manifest `schedule` 读取 cron、投递模式和群目标，按插件代注册 handler。
`app_scheduling.py` 执行 `broadcast`、`targeted` 与 `silent` 契约，统一校验目标并进入 OneBot 投递边界。
`durable_fanout.py` 为多目标通知记录持久进度，进程重启后从检查点继续。

插件排程发布与插件代绑定。
卸载或重载时，旧代排程先停止触发，再进入资源排空。

---

## 🧩 插件内嵌服务

复合插件可在 `init()` 中创建内部服务，并在 `shutdown()` 中关闭资源。
Pendo 使用该模式管理 FastAPI、SQLite、前端资源、提醒调度和聊天命令。

内嵌服务遵循以下所有权：

- 插件生命周期拥有服务启动与停止。
- 插件数据目录拥有数据库与持久文件。
- Core Context 提供日志、配置、密钥和发送能力。
- 服务内部按数据库、认证、API、任务和前端资源划分模块。

---

## 🧠 小青运行时

`xiaoqing_chat`、`xiaoqing_agent`、`xiaoqing_life` 和 `xiaoqing_memory` 都是普通插件，按同一套 Manifest、Context 和代际规则加载。
四者合起来构成小青这个角色的运行时：其他插件通过命令目录被她使用，Core 为这四个插件之间的协作提供专用的服务桥和 capability。

本节只讲四者之间的分工、服务和数据归属。
各插件怎么开启、有哪些命令和配置、具体怎么表现，见 [插件使用手册的智能聊天与语音](09-plugins.md#-智能聊天与语音) 和各插件自己的 README、ARCHITECTURE。

| 插件 | 拥有 | 提供的服务 | 关闭或不在时 |
|---|---|---|---|
| `xiaoqing_chat` | 聊天记录、回复时机和人设 | `core.observe_outgoing_action`；以 `smalltalk_provider` 的身份接收消息 | 角色不参与聊天 |
| `xiaoqing_agent` | 任务、工作目录和任务记录 | `agent.handle_turn`、`agent.report_delivery` | 聊天保持原有行为，不办事 |
| `xiaoqing_life` | 经历、词条、表达、小课题、作品、档案和梗图 | `life.turn_context`、`life.export_memes` | 聊天的提示词与没装时一致 |
| `xiaoqing_memory` | 原始记录、笔记和页面 | `memory.record`、`memory.recall`、`memory.search`、`memory.forget` | 三个插件照常工作，只是没有长期记忆 |

### 聊天与 agent

`xiaoqing_agent` 通过 Core 服务表和 capability 与其他插件协作：

```text
xiaoqing_chat ──agent.handle_turn（decide / act）──► xiaoqing_agent
      ▲                                                  │
      └──────────────── agent.report_delivery ◄─────────┤
                                                         ├─ command_runner ───────► Dispatcher.run_command_text
                                                         ├─ local_tools（跟随 /shell）
                                                         ├─ qingssh.agent_command ─► qingssh（跟随 /ssh）
                                                         └─ codex.agent_task ─────► codex（跟随 /codex）
```

- 明确召唤时，聊天把这一轮连同人设与说话方式交给 agent 判断，判断与聊天生成同时进行；agent 照这份人设写回复，接手后的当轮回复仍由聊天侧随回执发出并记录。
- agent 能做的就是发起人自己能做的。`command_runner` 以发起人身份只走命令阶段，在一次性会话空间里执行，命令返回的发送回执原样交回 agent，由它随附件的实际发送结算；每次调用带独立的操作标识。
- Codex 与 QingSSH 的长时间执行在各自插件的执行通道之外等待，串行插件不会被一条长命令占住。

### 聊天与网上生活

`xiaoqing_life` 负责小青自己上网，`xiaoqing_chat` 只取用结果：

```text
Core scheduler ── xiaoqing_life.tick（每 15 分钟）──► xiaoqing_life ── 后台上网 ──► 固定域名的只读 GET
xiaoqing_chat ── life.turn_context（每轮回复前）────► xiaoqing_life（只读内存，不碰磁盘、网络、模型）
xiaoqing_chat ── life.export_memes（每小时 07、37 分）► xiaoqing_life
```

- 聊天每轮回复前取和话题相关的一两点放进提示词；超时、报错、开关关闭或插件不在时当作没有。
- 请求不含用户 ID 与群号。数据只关于小青自己，不按用户身份门控，系统主体也可用。Core 只在调用方声明 `uses_services` 时签发 `capabilities.life_context`。

### 统一记忆

聊天、办事和网上生活经同一套服务读写 `xiaoqing_memory`：

```text
xiaoqing_chat  ── memory.record（消息落盘后、回复投递后）──┐
xiaoqing_agent ── memory.record（办事结束时）──────────────┼──► xiaoqing_memory ── 事件 → 笔记 → 页面
xiaoqing_life  ── memory.record（每次定时检查后）──────────┘          ▲
xiaoqing_chat  ── memory.recall / search / forget ───────────────────┤（纯内存取用，不调模型）
xiaoqing_agent ── memory.recall（开新办事会话时）────────────────────┘
Core scheduler ── xiaoqing_memory.tick（每 30 分钟）──► 夜间或积压时整理
```

- 记录分三个范围：`global`（她自己）、`g<群号>`、`u<QQ号>`。私聊的事不进群，一个群的事不进另一个群；他在群里说过的自己的事可以带进和他本人的私聊。可见性只在记忆插件里判断一处。
- Core 签发 `capabilities.memory` 时把调用方名字、主体类型和用户主体所在的会话写进载荷，调用方填的同名字段无效；用户主体发起的调用只能作用于它所在的会话。各调用方能写的类型由记忆插件固定。
- 来源链里有网页文字的内容不会交给 `xiaoqing_agent`。
- 每轮的网上生活由 `life.turn_context` 挑，不经过记忆；她的自我页（记忆整理的）也写到网上看过的东西，两边说到同一条经历、查明白的事或日记时，按 life 的条目键只留 life 给的细节。群里这两三天的话题词由记忆整理时记下，聊天转交给 life 当上网的方向。

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

`XiaoQingApp.shutdown_errors` 以只读快照暴露最近一次关闭中未能收敛的资源错误。
进程入口等待 `XiaoQingApp.stop()` 完成后检查该快照，存在错误时以失败状态退出。
运行验收同时检查退出状态、关闭完成日志和端口释放。

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
