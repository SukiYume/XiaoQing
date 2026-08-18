# 🧠 07 - 高级主题

本章面向需要扩展长任务、调度、声明式服务、内嵌 Web、缓存、并发和生产运行方式的开发者。基础插件流程见 [插件开发指南](03-plugin-development.md)。

---

## ⚙️ 私有 API 依赖登记

项目将第三方私有 API 集中登记，并为每个调用点配置能力探针、异常隔离、稳定回收路径和升级回归测试。

| 位置 | 私有 API | 用途 | 保护措施 |
|---|---|---|---|
| `core/plugin_manager_support.py` | `importlib._bootstrap._get_module_lock` | 插件热加载期间复用 CPython 模块锁 | `getattr` 探针、行为验证、restart-only 模式 |
| `core/scheduler_compat.py` | APScheduler 内部 job store、executor、timer 和 pending future | 调度器重载与关闭 | 版本探针、调用约定验证、有界停机 |
| `plugins/jupyter/jupyter_manager.py` | `_invoke_cleanup` 回调反射与 Jupyter 清理方法 | 多版本内核终止 | 受限签名、候选隔离、清理报告 |
| `plugins/codex/runner.py` | `process._transport.get_pipe_transport` | 子进程管道回收 | 属性与类型探针、异常隔离、进程树回收 |

依赖升级流程包含私有 API 探针测试和完整关闭场景。

---

## 💬 多轮会话模式

Session 适合等待用户下一步输入的交互：

```text
命令入口
  → create_session()
  → 用户后续消息
  → handle_session()
  → update_session()
  → end_session()
```

示例：

```python
async def handle(command, args, event, context):
    await context.create_session(
        {"step": "number", "attempts": 0},
        timeout=300.0,
    )
    return segments("请输入一个数字")


async def handle_session(text, event, context, session):
    value = parse_int(text, minimum=1, maximum=100)
    if value is None:
        return segments("请输入 1 到 100 的整数")

    async def commit(working):
        working.set("attempts", working.get("attempts", 0) + 1)

    await context.update_session(commit)
    return segments(f"收到：{value}")
```

会话 callback 处理单一会话键。跨用户或跨群协调在 callback 外完成，并通过业务数据库或专用锁提交。

---

## 📌 后台任务模式

长任务使用插件自有队列：

```text
命令入口
  → 校验任务
  → 写入队列与持久状态
  → 返回 job ID

后台 worker
  → 执行任务
  → 保存 artifacts
  → context.send_action()
  → 提交完成状态
```

后台队列应明确以下边界：

- 单用户、单标签和全局队列容量
- 并行任务上限
- 任务取消与进程树回收
- 重启恢复与幂等键
- 输出字节、文件和消息预算
- 插件卸载排空时间
- 主动发送回执与业务状态提交顺序

Codex 插件提供该模式的完整实现。

---

## ⏰ 定时任务

Manifest 声明 cron：

```json
{
  "schedule": [
    {
      "id": "example.daily",
      "handler": "scheduled_daily",
      "cron": {"hour": 8, "minute": 0},
      "group_ids": [123456789],
      "description": "每日摘要",
      "enabled": true
    }
  ]
}
```

入口函数：

```python
async def scheduled_daily(context):
    return segments("今日摘要")
```

常用 CronTrigger 字段：

| 场景 | `cron` |
|---|---|
| 每天 08:00 | `{"hour": 8, "minute": 0}` |
| 每两小时 | `{"hour": "*/2"}` |
| 工作日 09:00 | `{"day_of_week": "mon-fri", "hour": 9}` |
| 每月 1 日 | `{"day": 1, "hour": 0}` |

多目标业务通知可使用 `durable_fanout.py` 记录每个目标的投递进度。计划任务更新与插件代际采用同一发布边界。

---

## 🧩 声明式服务

Core 维护封闭服务表，Manifest 声明所有者和调用方：

```json
{
  "services": [
    {
      "name": "voice.synthesize_text",
      "callback": "synthesize_text",
      "callers": ["smalltalk"]
    }
  ]
}
```

消费插件声明：

```json
{
  "uses_services": ["voice.synthesize_text"]
}
```

Core 校验服务名、所有者、调用方、callback 和所需 capability。服务调用进入提供插件的执行 gate，并受代际排空保护。

新增服务桥接需要同步更新：

1. `PluginServiceName`
2. 服务所有者与调用方契约
3. Protocol 或 capability 类型
4. Manifest
5. 授权与生命周期测试

---

## 🌐 内嵌 Web 服务

复合插件可在自身生命周期内运行 FastAPI 或其他异步服务：

```python
async def init(context):
    server = await start_web_server(context)
    context.state["web_server"] = server


async def shutdown(context):
    server = context.state.get("web_server")
    if server is not None:
        await server.stop()
```

推荐内部边界：

```text
plugin/
├── main.py              # Core 生命周期适配
├── config.py            # 插件配置解析
├── services/            # 业务服务
├── web/
│   ├── server.py        # Web 生命周期
│   ├── api/             # 路由
│   ├── auth.py          # 认证与会话
│   └── static/          # 前端资源
└── data access          # 数据库与仓储
```

插件生命周期拥有 Web Server，数据库层拥有事务，认证层拥有凭据与会话，路由层完成请求校验与响应格式化。

Pendo 的 [架构说明](../plugins/pendo/ARCHITECTURE.md) 展示完整实例。

---

## 🌐 URL 处理扩展

URL Parser 在 Dispatcher 的 URL 阶段接收单 URL 消息。处理流程：

1. 规范化 URL。
2. 校验 scheme、DNS、目标网段和重定向。
3. 设置连接、总时长和响应大小预算。
4. 校验内容类型。
5. 提取有界标题、摘要和媒体。
6. 生成 OneBot 消息段。

远程抓取使用 `safe_http.py` 与 `bounded_http.py`，业务插件只处理经过安全边界的响应内容。

---

## 💬 Smalltalk Provider

`plugins.smalltalk_provider` 选择全局闲聊插件。Provider 实现：

```python
async def handle_smalltalk(text, event, context): ...
```

Provider 可组合以下阶段：

- 注意力与触发判断
- 用户、群和会话频率控制
- 上下文与媒体构建
- 行为规划
- AI route
- 候选回复检查
- 消息段发送
- 记忆与表达学习

命令、URL、Session 与普通聊天在 Dispatcher 中拥有独立阶段。Provider 专注普通聊天候选消息。

---

## 🔄 并发与背压

Core 提供三层预算：

1. `max_concurrency`：Dispatcher 全局消息并发。
2. `plugin_execution`：按插件入口、队列、超时和熔断。
3. `run_sync()`：同步 worker、按插件 bulkhead 和全局公平队列。

插件内部并发使用有界任务组：

```python
results = await gather_bounded(
    (fetch_one(item) for item in items),
    limit=4,
)
```

后台队列、HTTP 连接池、缓存、文件读取和输出均应设置容量或字节预算。过载场景返回插件公开错误，并保留 request ID。

---

## 💾 缓存与持久状态

### 内存缓存

`context.state` 适合插件代内缓存、任务引用和已解析配置。插件重载时由新代重新构建。

### 磁盘缓存

`BoundedFileCache` 提供：

- TTL
- LRU
- 条目数上限
- 总字节上限
- 原子元数据写入

缓存键使用规范化输入摘要。业务数据库与缓存采用各自的目录和恢复策略。

### 数据库

SQLite 插件推荐：

- 单一数据库所有者
- 显式事务
- schema 版本表
- 启动迁移
- `quick_check` 与备份验证
- 用户作用域查询条件
- WAL 文件一致备份

---

## 🔐 错误边界

| 边界 | 处理方式 |
|---|---|
| 用户输入 | 返回明确字段、范围和用法 |
| 第三方 HTTP | 超时、状态码、内容类型和响应大小分类 |
| AI Provider | route 重试、fallback、总预算和模型元数据 |
| 文件与数据库 | 原子写入、事务回滚、完整性校验和备份 |
| 后台任务 | 状态机、取消、进程树回收和重启恢复 |
| 生命周期 | 创建者回收资源，应用逆序关闭 |

插件日志记录业务标识、阶段、上游状态和 request ID。用户回复使用稳定公开错误码。

---

## ✅ 调试与验证

### DEBUG 日志

```json
{
  "log_level": "DEBUG",
  "log_to_console": true,
  "log_to_file": true
}
```

### 定点测试

```bash
python -m pytest tests/plugins/<plugin> -q
python -m ruff check plugins/<plugin> tests/plugins/<plugin>
python -m mypy plugins/<plugin>
```

### 完整 UAT

```bash
bash scripts/run_full_uat.sh --plan-only
bash scripts/run_full_uat.sh
```

---

## 🛡️ 生产运行

### Windows

```text
scripts/run-bot.vbs
  → scripts/run-bot-monitor.ps1
  → scripts/run_process_with_rotating_logs.py
```

双击 `scripts/stop-bot.vbs` 可停止该启动链管理的监控器、Bot 和 NapCat。停服模式持有同一仓库级互斥量，并在结束进程前复核 PID、进程名与绝对命令路径。CIM 身份读取经过三次短重试；提升权限创建的进程由一次 UAC 提升后的停止实例重新验证并回收。完成提示出现后可双击 `scripts/run-bot.vbs` 重启。

生产配置来源整体替换安排在停服窗口内。运行实例通过 `/set_secret` 管理已有 secret 路径；部署文件更新完成后重新启动，使公开配置与 secrets 在启动阶段组成已确认 revision。

### systemd

```ini
[Unit]
Description=XiaoQingBot
After=network-online.target

[Service]
Type=simple
WorkingDirectory=/path/to/XiaoQing
ExecStart=python main.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

将 `WorkingDirectory` 替换为实际项目目录。服务管理器提供可用的 `python`、依赖包与运行账户；项目启动入口保持为 `python main.py`。

### Docker

仓库 `Dockerfile` 提供容器构建入口。部署时挂载 `config/`、`data/` 与 `logs/`，并将 Inbound 端口连接到受控代理网络。

生产环境应为 Bot 进程设置专用用户、最小文件权限、明确网络出口、日志轮转、数据备份和健康检查。

---

## 🧭 下一步

- 组件所有权：[系统架构](02-architecture.md)
- 配置预算：[配置详解](06-configuration.md)
- 插件专项实现：[插件目录](09-plugins.md)
