# ⚙️ 04 - Core 模块

本章是 `core/` 的职责索引，帮助维护者从运行现象定位到代码所有者。组件关系见 [系统架构](02-architecture.md)，公开插件接口见 [API 参考](05-api-reference.md)。

---

## ⏰ 应用装配与生命周期

| 模块 | 职责 |
|---|---|
| `app.py` | `XiaoQingApp` 稳定门面、组件引用和公开启动入口 |
| `app_support.py` | 应用装配所需的纯解析与辅助函数 |
| `app_config_apply.py` | 配置 revision、敏感状态和运行参数的串行发布 |
| `app_plugin_context.py` | 插件 Context、principal、capability 和服务视图签发 |
| `app_lifecycle.py` | 启动事务、回滚、任务监督和逆序关闭 |
| `app_ingress.py` | Inbound Listener 候选绑定、提交和旧代排空 |
| `app_delivery.py` | OneBot Action 投递、回执和测试事件收集 |
| `app_identity.py` | 管理员状态、principal authority 和身份快照 |
| `app_scheduling.py` | 插件 schedule 的发布、替换和撤销 |
| `app_plugin_watch.py` | 插件 watcher 的启动、监督、退避和关闭 |
| `lifecycle.py` | 通用资源栈、异步回收和关闭预算 |

`XiaoQingApp` 负责所有权编排。具体业务逻辑位于对应服务模块，启动与关闭路径由 `app_lifecycle.py` 统一组织。

---

## ⌨️ 消息分发与命令

| 模块 | 职责 |
|---|---|
| `message.py` | OneBot 消息段标准化、文本提取和消息属性解析 |
| `dispatcher.py` | `MessageContext` 构建、A–G 流程、Session 与插件调用 |
| `router.py` | Manifest 命令树、触发词索引、子命令解析和冲突检测 |
| `args.py` | `ParsedArgs` token 与选项解析 |
| `models.py` | OneBot、Manifest、命令、服务和配置数据模型 |
| `auth.py` | Bot 管理员、群管理员和命令权限判断 |
| `public_errors.py` | 内部异常到公开错误码和 request ID 的映射 |

Dispatcher 的固定流程为：处理门控、URL、Bot 名称、会话、命令、命令兜底、闲聊。Router 从已验证 Manifest 构建只读命令目录，并把解析结果放入插件 Context。

---

## 🧩 插件管理

| 模块 | 职责 |
|---|---|
| `plugin_manager.py` | 插件管理门面和高层加载操作 |
| `plugin_manager_support.py` | 插件定义、加载记录、Manifest 支撑类型和工具 |
| `plugin_watcher.py` | 目录发现、Manifest 校验、源码快照和文件变化收敛 |
| `plugin_generation.py` | 插件代际创建、发布、回滚、排空和回收 |
| `plugin_runtime.py` | 运行入口、声明式服务、调用授权和代际守卫 |
| `plugin_execution.py` | 按插件并发、队列、超时、熔断、同步 bulkhead 和公平调度 |
| `plugin_data.py` | 项目级数据目录、布局升级和路径所有权 |

插件加载按目录规范名排序。Watcher 构建稳定快照，Generation 负责原子发布，Runtime 负责调用边界，Execution 负责资源预算。

Manifest `concurrency` 由 `plugin_execution.py` 落实：

- `parallel` 使用插件配置的并发上限。
- `sequential` 为插件入口提供单并发执行。

Python 模块依赖在导入前验证。命令触发词冲突在发布命令目录前报告。

---

## 🧩 插件公开能力

| 模块 | 职责 |
|---|---|
| `context.py` | `PluginContext` 实现、作用域视图和便捷方法 |
| `interfaces.py` | Protocol、principal、capability 和跨模块接口 |
| `capabilities.py` | Core 签发能力的具体实现 |
| `plugin_base.py` | 消息段、Action、同步任务、文件和拆分工具 |

Context 由当前插件名、用户、群、request ID 和 principal 共同确定。普通插件获得当前命名空间的配置与 secret；Manifest capability 对应 Core 维护的窄特权对象。

---

## 💬 会话、调度与投递

| 模块 | 职责 |
|---|---|
| `session.py` | Session 快照、会话键、串行锁、超时和原子更新 |
| `scheduler.py` | APScheduler 装配、Manifest cron 注册和目标投递 |
| `scheduler_compat.py` | APScheduler 版本适配边界 |
| `delivery.py` | 进程内发送回执、目标化调度结果和 commit-after-ack |
| `durable_fanout.py` | 多目标通知的持久进度和恢复 |
| `async_keyed_lock.py` | 按业务键串行的异步锁 |

Session 管理用户驱动的多轮交互。Scheduler 管理时间驱动任务及其广播、目标化或静默投递模式。Delivery 与 Durable Fanout 管理业务状态提交和主动消息投递进度。

---

## 🌐 OneBot 与网络

| 模块 | 职责 |
|---|---|
| `onebot.py` | HTTP Action 发送器、主动 WebSocket Client 和连接退避 |
| `server.py` | HTTP `/event`、WebSocket `/ws`、`/health`、`/metrics` 和 Inbound 调度 |
| `inbound_policy.py` | Listener 地址、loopback、代理信任和 token 策略 |
| `safe_http.py` | URL、DNS、目标网段、重定向和出站请求安全校验 |
| `bounded_http.py` | 响应状态、内容类型、字节预算和流式读取 |
| `image_validation.py` | 图片类型、尺寸、像素和解码预算 |

Inbound HTTP 与 WebSocket 使用同一鉴权和会话排序。出站远程内容先经过 `safe_http.py` 解析目标，再由 `bounded_http.py` 执行有界读取。

---

## 🔐 配置、AI 与安全

| 模块 | 职责 |
|---|---|
| `config.py` | JSON 读取、来源状态、只读快照、revision 和文件 watcher |
| `ai.py` | Provider、模型 profile、插件 route、重试、fallback 和总预算 |
| `sensitive_audit.py` | 敏感字段审计、摘要和脱敏 |
| `atomic_store.py` | JSON 与字节数据的原子写入和恢复 |
| `bounded_file_cache.py` | TTL、LRU、条目数和字节数受限的磁盘缓存 |

配置分为公开配置和 secrets。应用层按 revision 发布快照。AI Service 从同一 revision 解析 provider、模型、插件 route 和凭据。

---

## 📌 基础设施

| 模块 | 职责 |
|---|---|
| `logging_config.py` | 控制台、文件、颜色、轮转和日志格式 |
| `metrics.py` | 消息、插件、错误、延迟和队列指标 |
| `clock.py` | 时区感知时间和可测试时钟 |
| `constants.py` | Core 共享边界常量 |
| `exceptions.py` | 领域异常类型 |
| `version.py` | 从项目元数据或 wheel 元数据解析运行时版本 |
| `__init__.py` | Core 包边界 |

---

## 🏗️ 关键所有权

| 资源 | 创建者 | 关闭者 |
|---|---|---|
| 共享 HTTP Session | `XiaoQingApp` | `app_lifecycle.py` |
| Inbound Server | `app_ingress.py` | `app_lifecycle.py` |
| 主动 WebSocket Client | `app_lifecycle.py` | `app_lifecycle.py` |
| Plugin Generation | `plugin_generation.py` | `plugin_generation.py` |
| 插件内嵌服务 | 插件 `init()` | 插件 `shutdown()` |
| Scheduler | `app_lifecycle.py` | `app_lifecycle.py` |
| Config Watcher | `app_lifecycle.py` 调用 `ConfigManager.watch()` | `app_lifecycle.py` |
| Plugin Watcher | `app_plugin_watch.py` | `app_plugin_watch.py` |

资源创建和回收由同一所有权边界配对。应用关闭按依赖关系逆序执行。

---

## 🩺 排障定位

| 现象 | 首要模块 | 关联模块 |
|---|---|---|
| OneBot 连接与重连 | `onebot.py` | `app_config_apply.py`, `app_lifecycle.py` |
| Inbound 鉴权与端口 | `server.py` | `inbound_policy.py`, `app_ingress.py` |
| 命令匹配 | `router.py` | `dispatcher.py`, `models.py` |
| 群聊参与 | `dispatcher.py` | Smalltalk Provider 插件 |
| 插件加载与重载 | `plugin_watcher.py` | `plugin_generation.py`, `plugin_runtime.py` |
| 插件超时与过载 | `plugin_execution.py` | `dispatcher.py` |
| Session 状态 | `session.py` | 对应插件 session handler |
| 定时消息 | `scheduler.py` | `durable_fanout.py`, 对应插件 handler |
| AI route | `ai.py` | `config.py`, `context.py` |
| 外部 URL | `safe_http.py` | `bounded_http.py`, 对应插件 |

---

## 🧭 下一步

- 公开接口签名：[API 参考](05-api-reference.md)
- 完整消息阶段：[消息处理流程](08-message-flow.md)
- 配置字段：[配置详解](06-configuration.md)
