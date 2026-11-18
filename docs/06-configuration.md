# 🛠️ 06 - 配置详解

中文 | [English](en/06-configuration.md)

本章说明 XiaoQing Core 的公开配置、敏感配置、AI 注册表、插件命名空间、网络边界和热重载语义。
插件专项字段由各插件 README 维护。

---

## ⚙️ 配置文件

| 文件 | 内容 | 管理方式 |
|---|---|---|
| `config/config.json` | 公开运行设置、模型 profile 和插件配置 | 从 `.example` 复制后按部署环境编辑 |
| `config/secrets.json` | token、管理员身份、AI Key 和插件凭据 | 保存在部署主机并限制文件读取权限 |
| `config/config.json.example` | 当前公开配置结构 | 随源码版本维护 |
| `config/secrets.json.example` | 当前敏感配置结构 | 随源码版本维护 |

创建本机配置：

```bash
cp config/config.json.example config/config.json
cp config/secrets.json.example config/secrets.json
```

Core 将两个文件组合成带 revision 的只读设置快照。
插件通过作用域 Context 读取自身命名空间。

`config.json` 顶层 `ai` 对象维护统一模型注册表，顶层 `plugins` 对象维护插件公开命名空间。
`secrets.json` 使用同名 `ai` 与 `plugins` 对象保存对应凭据。

### 启动时读不到有效配置

进程启动时第一次读取 `config/config.json`。
文件缺失、不是合法 JSON、读不了或校验失败时进程不启动，记下原因后以退出码 1 结束；运行目录下的 `logs/` 建不了时同样如此。
这时配置里的日志出口还没换上，诊断只写到控制台（标准输出），`logs/xiaoqing.log` 里没有：

| 事件 | 含义 |
|---|---|
| `config.source.missing source=config` | 文件不存在 |
| `config.source.invalid source=config status=… error=…` | `status` 为 `invalid` 时内容无效（不是合法 JSON、超过 8 MiB 或校验失败），为 `unavailable` 时读不了或读取期间一直在变；JSON 语法错误带出错的行列号 `error.lineno`、`error.colno`，校验失败带出错字段数 `field_errors` |
| `config.field.invalid source=config field=<字段路径> error_type=<错误类型>` | 校验失败时每个出错字段一条，最多 20 条；`error_type` 是 pydantic 的错误类型，例如 `string_type`、`greater_than_equal` |
| `process.failed error=… error.at=…` | 进程入口记的最后一行，CRITICAL 级，附回溯 |

例如 `bot_name` 写成数字、`ws_queue_size` 写成 `0` 时（节选）：

```text
2026-11-17 09:30:12 [ERROR] [-] core.config: config.source.invalid source=config status=invalid error=ConfigLoadError error.at=core/config.py:232 error.cause=ValidationError field_errors=2
2026-11-17 09:30:12 [ERROR] [-] core.config: config.field.invalid source=config field=bot_name error_type=string_type
2026-11-17 09:30:12 [ERROR] [-] core.config: config.field.invalid source=config field=ws_queue_size error_type=greater_than_equal
2026-11-17 09:30:12 [CRITICAL] [-] __main__: process.failed error=ConfigLoadError error.at=core/app.py:98
```

配置里的值、地址和异常消息都不打印。
`secrets.json` 的问题记成同样的事件（`source=secrets`），不阻止启动，凭据按 [配置热重载](#️-配置热重载) 里的 fail-closed 规则处理。

Windows 生产启动链下这些行在 `logs/bot-monitor.log` 里。
Bot 退出后监控器按退避间隔重新启动它（10 秒起，每次翻倍，最长 300 秒），配置改好之前这些行反复出现。
监控器自己启动时也读取 `config.json`，文件缺失或不是 JSON 对象时监控器报错退出，不启动 Bot。

---

## ⚙️ Core 配置

### 机器人与命令

| 字段 | 类型 | 示例值 | 说明 |
|---|---|---|---|
| `bot_name` | string | `"小青"` | Bot 的唯一运行时名称，用于名称召唤、群聊门控、聊天人格、历史角色标签和默认上线通知 |
| `command_prefixes` | 非空 string[]，每项非空 | `["/"]` | 命令前缀列表；不写这一项时是 `["/"]` |
| `require_bot_name_in_group` | boolean | `true` | 群聊普通文本的 Bot 名称门控 |
| `default_group_ids` | positive integer[] | `[123456789]` | `broadcast` / `targeted` schedule 未指定 `group_ids` 时的群目标 |
| `timezone` | IANA timezone | `"Asia/Shanghai"` | Scheduler、`context.now()` 和业务日期时区 |

`command_prefixes` 可配置多个前缀；空列表或含空串的列表会被当作无效配置拒绝。
Manifest `triggers` 保存纯命令词，Router 在消息解析阶段处理前缀。
这五项通过 `/reload` 发布到新 revision，并触发插件后台重载。

### OneBot 通信

| 字段 | 类型 | 示例值与范围 | 说明 |
|---|---|---|---|
| `enable_ws_client` | boolean | `false` | 主动 OneBot WebSocket Client 开关 |
| `onebot_ws_uri` | absolute URL | `ws://127.0.0.1:11000/ws` | 主动 WebSocket 地址，支持 `ws` 与 `wss` |
| `onebot_http_base` | absolute URL | `http://127.0.0.1:11001` | OneBot HTTP Action 地址，支持 `http` 与 `https` |
| `ws_queue_size` | integer | `200`，`1..10000` | 主动 WS 事件队列，以及 Inbound 等待 backlog |
| `enable_inbound_server` | boolean | `true` | HTTP/WS Inbound Server 开关 |
| `inbound_http_base` | plaintext listener URL | `http://127.0.0.1:12000` | HTTP `/event` Listener 基址，要求显式端口与根路径 |
| `inbound_ws_uri` | plaintext listener URL | `ws://127.0.0.1:12000/ws` | WebSocket Listener 地址，要求显式端口 |
| `inbound_ws_max_workers` | integer | `8`，`1..128` | Inbound 事件处理 worker 与 WS fan-out 并发上限 |
| `inbound_ws_broadcast_timeout_seconds` | number | `5.0`，`0 < value <= 300` | 单个 WebSocket 广播预算 |
| `inbound_trusted_tls_proxy` | boolean | `false` | 受控代理网络的明文 Listener 授权 |

启用 HTTP 或 WebSocket Inbound 时，`secrets.json` 顶层 `inbound_token` 使用非空字符串。
HTTP 与 WebSocket 共享该 token、接纳队列和会话排序。

HTTP 上报端通过 `Authorization: Bearer <inbound_token>` 提交认证。
标准上报的回复通过 `onebot_http_base` 配置的 Action API 发送，并在实际投递成功后返回 `{}`。
因此 HTTP 上报部署同时配置可达的回复通道及其 `onebot_token`。
开发验收程序可显式提交 `X-XiaoQing-Response-Mode: actions` 获取动作列表。
调用者承担动作执行责任。
OneBot `secret` / `X-Signature` 对应 HMAC 签名，当前入站接口验证 Bearer token。

主动 WebSocket 与 HTTP Action 通过 `secrets.json` 顶层 `onebot_token` 使用 Bearer 鉴权。
有效 secrets 快照中的空字符串表示双方约定的匿名 OneBot 连接。

Inbound 的接纳容量为 `inbound_ws_max_workers + ws_queue_size`。
同一私聊用户、同一群与用户组合各自按接纳顺序串行执行，各会话键之间共享 worker 并行处理。
达到容量时 HTTP 返回过载状态，WebSocket 返回过载响应。

### Inbound 网络边界

默认 Listener 地址使用 loopback。
公网入口采用以下路径：

```text
公网 HTTPS/WSS
  → Nginx / Caddy TLS 终止与访问控制
  → loopback HTTP/WS
  → XiaoQing Inbound
```

`inbound_http_base` 使用 `http://`，`inbound_ws_uri` 使用 `ws://`。
XiaoQing Listener 负责明文 HTTP/WS 协议，TLS 终止由外层代理或网络层提供。
Listener 绑定非 loopback 主机（例如跨容器的受控网络）时，必须同时设置 `inbound_trusted_tls_proxy: true`，并通过主机防火墙把入口限定到代理来源。

### 运行时与数据

| 字段 | 类型 | 默认值与范围 | 生效方式 | 说明 |
|---|---|---|---|---|
| `max_concurrency` | integer | `5`，`1..1024` | 热重载 | Dispatcher 全局活动消息上限 |
| `data_root` | path string | `"data"` | 重启 | 插件运行数据根目录 |
| `enable_plugin_watcher` | boolean | `false` | 热重载 | 插件文件 watcher 开关 |
| `enable_plugin_schedules` | boolean | `true` | 重新加载 | 插件定时任务总开关；隔离运行（如全量 UAT）设为 `false`，跨过计划时间也不会调用外部账号或投递消息 |
| `plugin_poll_interval` | number | `3600`，`0.01..86400` | 热重载 | 插件文件轮询秒数 |
| `session_timeout` | number | `300`，`0.001..604800` | 热重载 | Session 默认空闲秒数 |

`data_root` 可使用项目相对路径或部署主机绝对路径，并保持在 `plugins/` 源码树之外。
每个插件获得 `data_root/<plugin_name>/`。
修改 `data_root` 后重启主进程。

插件 watcher 在启动时运行解释器能力探针。
通过探针的环境支持热重载，restart-only 环境通过重启进程应用源码变化。
Watcher 监控插件 Python、根目录 JSON 与 Manifest `watch_files`，只发布通过完整稳定快照校验的新插件代。

### Windows 生产启动

| 字段 | 示例 | 约束 | 说明 |
|---|---|---|---|
| `napcat_account` | `"1234567890"` | 空字符串或 5～20 位十进制数字 | NapCat 启动位置参数 |
| `mkl_threading_layer` | `"TBB"` | 空字符串或最长 64 字符的字母、数字、点、下划线和连字符 | Bot 子进程的 `MKL_THREADING_LAYER` |

`scripts/run-bot-monitor.ps1` 在创建子进程前读取这些字段。
启动链调用当前 `PATH` 中的 Python，并保持部署环境对解释器和依赖的所有权。
字段修改在下次启动监控链时生效。

`scripts/stop-bot.vbs` 是 Windows 双击停服入口。
它调用监控器的 `-Stop` 模式，通过仓库级互斥量阻止停服期间产生新实例，并按当前仓库脚本路径与 NapCat 可执行文件路径回收进程树。
CIM 命令行读取采用三次有界重试。
提升权限的 SSH、计划任务或管理员终端创建进程时，普通桌面入口显示一次 UAC，提升后的停止实例重新校验 PID、进程名和绝对命令路径。
停服完成后可双击 `scripts/run-bot.vbs` 重新启动。

---

## ⚙️ 插件执行配置

`plugin_execution` 同时管理异步入口与同步阻塞任务：

```json
{
  "plugin_execution": {
    "timeout_seconds": 60,
    "parallel_limit": 4,
    "admission_queue_limit": 64,
    "sync_parallel_limit": 1,
    "sync_queue_limit": 16,
    "failure_threshold": 3,
    "cooldown_seconds": 60,
    "drain_timeout_seconds": 5,
    "global_sync_queue_limit": 256,
    "overrides": {
      "arxiv_filter": {"timeout_seconds": 600}
    }
  }
}
```

| 字段 | 默认值 | 范围 | 说明 |
|---|---|---|---|
| `timeout_seconds` | `60` | `0` 或 `0.1..86400` | 单次插件入口预算。`0` 由插件自身生命周期控制 |
| `parallel_limit` | `4` | `1..1024` | `parallel` 插件入口并发 |
| `admission_queue_limit` | `64` | `0..10000` | 插件入口等待队列 |
| `sync_parallel_limit` | `1` | `1..3` | 单插件同步 worker 并发 |
| `sync_queue_limit` | `16` | `0..10000` | 单插件同步等待队列 |
| `failure_threshold` | `3` | `1..10000` | 熔断计数阈值 |
| `cooldown_seconds` | `60` | `0.1..86400` | 熔断冷却时间 |
| `drain_timeout_seconds` | `5` | `0.1..3600` | 卸载排空预算 |
| `global_sync_queue_limit` | `256` | `1..100000` | 全局同步等待队列 |
| `overrides` | `{}` | object | 按插件覆盖上述策略 |

Manifest `concurrency: sequential` 将该插件入口并发固定为 1。
`parallel` 使用 `parallel_limit`。
同步库通过 `run_sync()` 进入同步预算。

`admission_queue_limit` 与 `sync_queue_limit` 表示等待槽数量，值 `0` 对应零等待槽。
插件入口总接纳容量等于活动槽与等待槽之和。
`overrides.<plugin_name>` 可提供任意策略子集，省略字段继承全局策略。
在 `plugin.json` 里声明了 `execution_timeout_exempt` 能力的插件（codex、qingssh、jupyter、shell）入口预算默认就是 `0`，不用在这里再写。
JSON `null` 会触发配置校验错误。
失败次数达到 `failure_threshold` 后执行 gate 进入 `cooldown_seconds` 冷却，插件重载使用 `drain_timeout_seconds` 等待当前代收尾。

---

## ⚙️ 日志配置

日志设置集中在顶层 `logging` 对象：

```json
{
  "logging": {"level": "INFO", "levels": {}, "console": true, "file": true, "keep_days": 5}
}
```

| 字段 | 类型 | 默认值与范围 | 说明 |
|---|---|---|---|
| `level` | string | `"INFO"`；`DEBUG`、`INFO`、`WARNING`、`ERROR`、`CRITICAL` | Core、插件、脚本和进程入口的日志级别；第三方库见下文 [级别](#级别) |
| `levels` | object | `{}` | 按 logger 名单独设级别，取值同 `level` |
| `console` | boolean | `true` | 输出到标准输出 |
| `file` | boolean | `true` | 写入 `logs/xiaoqing.log`；和 `console` 都关时不输出任何日志 |
| `keep_days` | integer | `5`，`1..365` | 轮转后保留的历史文件份数 |

`logging` 对象里出现其他字段、级别名不是大写或 `keep_days` 越界时，配置校验报错。
日志 handler 在进程启动时创建，修改本节字段后重启主进程；启动日志里 `日志系统初始化完成 level=… dir=… keep_days=…` 这一行显示生效的设置。
读到配置之前，进程入口只开控制台输出，级别 `INFO`，行格式相同；读到配置后换成本节设置的出口。

日志只有 `logs/xiaoqing.log` 一个文件，位于运行目录下，每天午夜轮转，保留 `keep_days` 份历史文件，`ERROR` 及以上的记录也在其中。
控制台在标准输出是终端时按级别着色，重定向到文件或管道时不着色。
Windows 生产启动链把 Bot 的标准输出另存为 `logs/bot-monitor.log`，生产配置设 `"console": false`，同一条日志只写一份；读到配置之前写到控制台的几行（例如 [启动时读不到有效配置](#启动时读不到有效配置) 的诊断）只在 `bot-monitor.log` 里。

### 级别

`level` 管项目自己的 logger：`core`、`plugins`、`scripts` 和进程入口（`main`、`__main__`）。
其他 logger 都属于第三方库，级别是 `WARNING`；`level` 比 `WARNING` 更严时跟着 `level`。
`levels` 按 logger 名单独设级别，项目的和第三方库的都能调高或调低。
键是 logger 名：Core 模块是 `core.<模块>`，插件模块是 `plugins.<插件名>.<模块>`，第三方库用库自己的 logger 名，写前缀就作用于其下所有模块。
例如 `{"plugins.xiaoqing_chat": "WARNING", "core.ai": "DEBUG"}` 只看聊天插件的警告和错误，同时打开 AI 调用参数的 DEBUG 日志；`{"websockets": "DEBUG"}` 打开 websockets 库自己的 DEBUG 日志。

### 日志行

文件里的每行是：

```text
2026-11-16 09:30:12 [INFO] [3f9a1c2e] plugins.hello.main (main.py:42): fetch.done status=ok items=3
```

控制台的行不带 `(文件名:行号)`。
第二个方括号是 request ID：每条消息、每条代为执行的命令和每次定时任务运行各有一个，不属于任何请求的行是 `-`。
消息部分是事件名加字段，字段按 [插件开发指南的日志与指标](03-plugin-development.md#日志与指标) 的规则输出：标识和标签明文，正文只记长度和指纹，凭据脱敏，异常记类型、位置和状态码一类的属性，不记异常消息。
消息里的换行和控制字符转义成 `\n`、`\r`、`\xNN`，超过 4000 个字符的消息截断并注明多出的字符数。

第三方库的记录不经过 `LogEvent`。
行里保留库代码写的消息模板，格式化参数按同一套字段规则换掉再填进去：数字和日期原样，异常写类型名，列表、字典等容器写 `[N items]`，文本和其他对象只记长度和指纹。
位置参数没有键名，看起来像标识的文本也只记长度和指纹；`%(name)s` 形式的参数按键名规则处理。
例如 APScheduler 跳过一次执行时写：

```text
2026-11-17 09:30:12 [WARNING] [-] apscheduler.scheduler: Execution of job "<45ch #25c1a32c>" skipped: maximum number of running instances reached (1)
```

换掉的参数填不进模板时（例如 `%d` 遇到换成指纹的文本），消息是模板加 ` <arguments do not match the format>`；库代码没带参数的消息原样写出。

带异常的记录在行后附回溯。
异常链上每个异常一段，每段最多保留最里面的 48 帧，写成 `文件:行 in 函数`，省掉了更外层的帧时先写一行 `…(+N earlier frames)`；段末一行是异常类型，带状态码一类的属性，例如 `core.ai.AIRequestError (status=504)`。
文件写成项目内、第三方包内或标准库内的相对路径，不带本机目录。
异常消息可能带正文或凭据，不写进日志；整段回溯不超过 8000 个字符。

### 未捕获的异常与出口故障

解释器和事件循环默认直接打印的异常也记成事件，带 `error=…` 字段和上面的回溯：

| 事件 | logger 与级别 | 来源 |
|---|---|---|
| `process.uncaught` | `core.log`，CRITICAL | 主线程里没有捕获的异常 |
| `thread.uncaught thread=<线程名>` | `core.log`，ERROR | 其他线程里没有捕获的异常 |
| `process.unraisable` | `core.log`，ERROR | 析构函数、垃圾回收等处无法向外抛出的异常 |
| `asyncio.unhandled kind=<种类> task=<任务名>` | `asyncio`，ERROR | 事件循环里没人处理的异常；`kind` 是 `task_exception`、`future_exception`、`callback_exception`、`pending_task_destroyed`、`unclosed_resource` 或 `other`，没有异常时不带 `error` |

线程名、任务名不是简单标识时不记。
进程入口在启动或运行以异常结束时记一条 CRITICAL 级的 `process.failed`，然后以退出码 1 结束。

日志出口写不出去时（例如磁盘满、文件被别的进程占着），在标准错误写一行诊断，不带记录内容。
下面是 Windows 上午夜轮转被挡住时的一行：

```text
2026-11-17 00:00:03 [ERROR] [-] core.log: log.output_failed handler=file stage=rollover error=PermissionError error.at=core/log.py:703 error.errno=13 error.winerror=32
```

`handler` 是 `console` 或 `file`，`stage` 是 `write` 或 `rollover`。
同一出口同一阶段第一次失败立即写，之后每分钟最多一行，带上这期间压下的次数 `suppressed`。
轮转失败时记录照常写进当前文件，下一条记录再试轮转。
Windows 生产启动链把标准错误另存为 `logs/bot-monitor-error.log`。

---

## 🧠 AI/VLM 注册表

AI 配置分两块，密钥单独放：

1. **模型**（`config.ai`）：`providers` 写服务地址，`models` 给每个要用的模型起一个名字。
2. **谁用哪个模型**（`config.plugins.<name>.ai.routes`）：给插件的每条 route 列出模型名，第一个是主模型，后面的依次兜底。
3. `secrets.ai.providers`：各 Provider 的 API Key。

采样温度、输出预算、超时和重试属于任务本身（闲聊要活、审查要稳、分流要快），由各插件在代码里声明默认值，主配置不用写；确实要改某一项时，再把那条 route 写成对象覆盖。

```json
{
  "ai": {
    "providers": {
      "openai":   {"api_base": "https://api.openai.com/v1", "max_tokens_field": "max_completion_tokens"},
      "deepseek": {"api_base": "https://api.deepseek.com"}
    },
    "models": {
      "gpt-terra":      {"provider": "openai", "model": "gpt-6.1-terra", "modalities": ["text", "image"], "request_defaults": {"reasoning_effort": "none"}},
      "deepseek-flash": {"provider": "deepseek", "model": "deepseek-flash", "modalities": ["text"], "request_defaults": {"thinking": {"type": "disabled"}}}
    }
  },
  "plugins": {
    "xiaoqing_chat": {
      "ai": {
        "routes": {
          "chat":    ["gpt-terra", "deepseek-flash"],
          "checker": ["gpt-terra", "deepseek-flash"]
        }
      }
    }
  }
}
```

### Provider

| 字段 | 类型 | 默认值 | 规则 |
|---|---|---|---|
| `api_base` | absolute HTTP(S) URL | 必填 | 包含 scheme 与主机，省略凭据、query 和 fragment |
| `endpoint_path` | absolute URL path | `/chat/completions` | 以 `/` 开头，省略 query 与 fragment |
| `proxy` | absolute HTTP(S) URL or empty string | `""` | 当前 Provider 的可选代理 |
| `max_tokens_field` | `max_tokens` 或 `max_completion_tokens` | `max_tokens` | 这家接口接收输出预算用的字段名。OpenAI 当前的模型只认 `max_completion_tokens`；route 里统一写 `max_tokens`，Core 发请求时换成这里指定的字段 |

### 模型

| 字段 | 类型 | 规则 |
|---|---|---|
| `provider` | string | 引用 `config.ai.providers` 中的名称 |
| `model` | string | 发送给上游 API 的模型名 |
| `modalities` | string[] | 非空集合，成员为 `text`、`image`、`audio` |
| `request_defaults` | object | 这个模型每次请求都带上的附加字段（如关闭思考）。Core 保留 `model`、`messages`、`stream`、采样参数和工具字段 |
| `fixed_sampling` | bool，默认 `false` | 模型自带固定采样、不接受 `temperature` / `top_p` 时设为 `true`，Core 不发这两个字段 |

同一个上游模型通常只需要定义一次。只有确实要两种固定用法（比如一个开思考、一个不开）时才起两个名字。

### 插件 route

route 的值可以是一个模型名、一个模型名列表，或一个对象：

```json
{
  "routes": {
    "triage":  "gpt-terra",
    "chat":    ["gpt-terra", "deepseek-flash", "glm-5.2"],
    "checker": {"models": ["gpt-terra", "deepseek-flash"], "timeout_seconds": 20}
  }
}
```

写成对象时可用的字段如下。取值顺序是：主配置里这条 route 写明的 > 插件声明的默认值 > 下表的 Core 默认值。

| 字段 | Core 默认值与范围 | 说明 |
|---|---|---|
| `models` | 必填，一个名字或非空列表 | 按顺序排列主模型与兜底模型 |
| `temperature` | 可选，`0..2` | route 采样温度 |
| `top_p` | 可选，`0..1` | route nucleus sampling |
| `max_tokens` | 可选，`1..1000000` | route 输出 token 上限 |
| `timeout_seconds` | `30`，`0.1..600` | 单次上游请求预算 |
| `total_timeout_seconds` | `max(timeout, 60)`，`0.1..1800` | 全部重试与兜底的总预算 |
| `max_retry` | `1`，`0..10` | 每个模型的重试次数 |
| `retry_interval_seconds` | `0.5`，`0..60` | 指数退避基数 |
| `fallback_on` | Core 默认错误集合 | 触发下一个模型的错误类别 |
| `request_defaults` | `{}` | route 级附加请求字段 |

内置插件声明的默认值（`declare_route_defaults`，在各插件代码里）：

| 插件 route | 用途 | temperature / top_p | max_tokens | 单次 / 总超时（秒） | 重试 |
|---|---|---|---|---|---|
| `xiaoqing_chat.chat` | 日常回复 | 0.8 / 0.6 | 768 | 15 / 60 | 1 |
| `xiaoqing_chat.reasoning` | 规划、记忆检索、摘要、表达学习、生活记录抽取 | 0.6 / 0.9 | 2048 | 15 / 45 | 1 |
| `xiaoqing_chat.checker` | 回复审查 | 0 / 0.8 | 8192 | 12 / 30 | 0 |
| `xiaoqing_chat.vision` | 读图 | 0.2 / 0.9 | 800 | 20 / 60 | 1 |
| `xiaoqing_agent.agent` | 办事执行 | 0.2 / — | — | 180 / 600 | 1 |
| `xiaoqing_agent.triage` | 分流 | 0.1 / — | — | 15 / 20 | 1 |
| `xiaoqing_agent.vision` | `read_image` 读本地图片 | 0.2 / — | 800 | 20 / 60 | 1 |
| `xiaoqing_life.reasoning` | 消化、研究、睡前整理 | 0.6 / 0.9 | 2048 | 30 / 120 | 1 |
| `xiaoqing_memory.reasoning` | 夜间整理：写笔记、并页面、核对 | 0.3 / 0.9 | 3000 | 60 / 180 | 1 |
| `xiaoqing_life.vision` | 梗图判定 | 0.2 / 0.9 | 800 | 20 / 60 | 1 |
| `pendo.parse` | 自然语言解析 | 0.3 / — | 1000 | 30 / 60 | 1 |
| `xiaoai.plan` | 米家操作规划 | 0 / — | 1600 | 25 / 28 | 0 |
| `ads_paper.summary` | 论文摘要 | 0.7 / — | 1200 | 60 / 90 | 0 |

### 聊天与 Agent 如何使用 route

XiaoQing Chat 从这些路由生成类型化的运行时参数快照。日常回复使用 `chat`；规划、记忆检索、摘要和表达学习使用 `reasoning`；回复审查使用 `checker`；图片理解使用 `vision`。Agent 的执行使用 `agent`，任务分流使用 `triage`（没配时复用 `agent`），`read_image` 使用 `vision`。常规插件调用沿用路由参数。`/xc model default` 自动选择 `routes.chat` 中第一个可用的模型。明确要求长文时会提高输出预算和超时；图片分析被截断时会提高输出预算。可选工作和整项 Agent 任务仍受各自的业务时限约束。

`plugins/xiaoqing_chat/config/xiaoqing_config.json` 管理人设、记忆和参与行为。`temperature`、`top_p`、`max_tokens`、前后台超时与重试、规划请求超时、回复审查预算和视觉请求参数都属于对应的 route，深度聊天的采样参数沿用聊天路由，默认模型由路由顺序决定。行为文件中出现这些 AI 字段或 `ai` 注册表时，加载报错并指出主配置位置。

`core.ai` 的 DEBUG 日志（`ai.request.settings`）记录最终采用的路由、模型、采样值、输出预算、请求超时和重试策略。记录内容限定为调用参数元数据，密钥和消息正文保持私密。只看这部分时在 `logging.levels` 里写 `{"core.ai": "DEBUG"}`。

### Provider secret

```json
{
  "ai": {
    "providers": {
      "deepseek": {
        "api_key": "replace-with-provider-key"
      }
    }
  }
}
```

每个 Provider 在 `secrets.ai.providers.<provider_name>.api_key` 保存 API Key。
模型级 `request_defaults` 先合入请求，route 级默认值随后覆盖，调用参数最后覆盖采样与预算字段。

AI Service 先调用 route 中首个具备密钥的 profile，并对 transport、timeout、rate limit、server error、model unavailable、invalid response 和 empty response 执行有界重试与顺序 fallback。
`fallback_on` 还可选择 `request_timeout` 与 `conflict`。
认证、权限、请求参数和配置错误既不重试也不换下一个模型，直接交给当前调用的插件处理。
`total_timeout_seconds` 覆盖整条模型链。

供应商凭据块缺失、`api_key` 缺失、空字符串和纯空白字符串都会使对应模型暂时不可用。Core 在请求前按顺序跳过这些条目，可选模型列表只包含可用项。显式固定到缺密钥模型、整条路由缺少可用密钥时会报配置错误。无效地址、未知模型引用和错误密钥类型会触发校验错误。

---

## ⚙️ 插件配置命名空间

公开插件设置位于：

```text
config.plugins.<plugin_name>
```

插件敏感设置位于：

```text
secrets.plugins.<plugin_name>
```

插件通过以下接口读取：

```python
context.get_config("path.to.value")
context.get_secret("path.to.value")
context.get_settings_snapshot()
```

### 只属于这个部署的设置

群号、QQ 号、服务器地址、本机路径这类只属于某个部署的值写在 `config/config.json`，凭据写在 `config/secrets.json`。
`plugin.json`、插件目录和小青的行为文件随仓库发布，保持通用，部署时与仓库版本一致。

| 路径 | 说明 |
|---|---|
| `plugins.<插件名>.schedules.<任务 ID>.group_ids` | 这个定时任务的目标群，优先于 `plugin.json` 条目和 `default_group_ids`，见 [调度字段](03-plugin-development.md#调度字段) |
| `plugins.minecraft.servers.<配置名>` | Minecraft 服务器的 `host`、`port`、`log_file`，RCON 密码在 `secrets.plugins.minecraft.<配置名>` |
| `plugins.xiaoqing_chat.<字段>` | 覆盖行为文件 `xiaoqing_config.json` 里的同名字段，逐层合并 |

`scripts/sync_to_remote.sh` 用本机的 `config/config.json` 覆盖部署机上的那份。本机这份漏写某个任务的 `group_ids` 时，任务不会停，而是改投 `plugin.json` 条目或 `default_group_ids` 里的群。
所以同步前，脚本经 ssh 只读取回部署机的 `config/config.json` 和各插件的 `plugin.json`，用 `scripts/check_schedule_targets.py` 分别算出两边每个会投递的定时任务发往哪些群：有差别时预览会列出来，`--apply` 拒绝同步，确认是有意的改动再加 `--accept-target-changes`。
本机运行比对用的 Python 默认是 `python`，可用 `PYTHON_BIN` 指定。

仓库的 `.githooks/pre-commit` 运行 `.githooks/check_private.py`：要提交的文件里出现本机 config 或 secrets 里的群号、账号、凭据、用户目录，或 `config/private_terms.txt` 列出的词（一行一个，比如群友昵称）时拒绝提交，只报文件和字段路径，不打印值。
文本文件整篇按文字查；图片、PSD 这类二进制文件按字节查元数据里常见的 UTF-8 和 UTF-16 写法，命中处前后紧挨着字母或数字的不算（那是压缩像素数据里碰巧出现的一段）。
克隆后执行一次 `git config core.hooksPath .githooks` 启用；`python .githooks/check_private.py --all` 检查全部文件。

### Core 插件选择

| 路径 | 示例 | 说明 |
|---|---|---|
| `plugins.smalltalk_provider` | `"xiaoqing_chat"` | Dispatcher 的全局闲聊 provider，可选 `smalltalk` 或 `xiaoqing_chat` |

### Chat 与 Pendo

| 路径 | 示例值 | 说明 |
|---|---|---|
| `plugins.chat.daily_user_limit` | `20` | 单用户每日成功调用额度，范围 `1..1000000` |
| `plugins.chat.daily_global_limit` | `100` | 全局每日成功调用额度，范围 `1..1000000` |
| `plugins.pendo.web_enabled` | `true` | Pendo Web 生命周期开关 |
| `plugins.pendo.web_host` | `"127.0.0.1"` | Web Listener 主机 |
| `plugins.pendo.web_port` | `12001` | Web Listener 端口，范围 `1..65535` |
| `plugins.pendo.web_session_cookie_secure` | `false` | HTTPS 部署使用 `true`，为浏览器会话 Cookie 添加 Secure |
| `plugins.pendo.web_demo_enabled` | `false` | Web Demo 空间开关 |
| `plugins.pendo.ai.routes.parse` | 模型名列表 | 自然语言结构化解析用的模型 |

Pendo 的五个 Web 字段按配置 revision 原子发布，监听地址变化会重建插件拥有的 Web Server。
登录 Code 与浏览器会话有效期为 7 天，Widget Token 有效期为 365 天。
[Pendo 配置与 Web](../plugins/pendo/README.md#-web-控制台) 说明认证、Demo 与数据目录。

### ADS Paper 与 XiaoQing Chat AI

| 路径 | 说明 |
|---|---|
| `plugins.ads_paper.ai.routes.summary` | `/paper summarize` 的论文 AI 摘要 route |
| `plugins.xiaoqing_chat.ai.routes.chat` | 主回复 route |
| `plugins.xiaoqing_chat.ai.routes.checker` | 回复检查 route |
| `plugins.xiaoqing_chat.ai.routes.reasoning` | 规划与推理 route |
| `plugins.xiaoqing_chat.ai.routes.vision` | 图片理解 route，模型需要 `image` modality |
| `plugins.xiaoqing_chat.ai.model_aliases` | 可选：`/xc model` 用的短名称到模型名的映射，不写时直接用模型名 |

四条 XiaoQing Chat route 各自指定模型顺序，调用参数的默认值见上文“插件 route”的表。
行为、参与、记忆和表达参数位于 `plugins/xiaoqing_chat/config/xiaoqing_config.json`，也可写在 `config.plugins.xiaoqing_chat` 下，两者逐层合并，config 中的值优先。
[XiaoQing Chat 配置](../plugins/xiaoqing_chat/README.md#️-行为配置) 逐组说明这些字段。

### XiaoQing Agent

有人叫小青做事时的任务代理，所有人可用、能力按各自权限，默认关闭：

| 路径 | 默认 | 说明 |
|---|---|---|
| `plugins.xiaoqing_agent.enabled` | `false` | 总开关。关闭时 `xiaoqing_chat` 照常回复，与没有安装 agent 时一致，修改后下一条消息生效 |
| `plugins.xiaoqing_agent.ai.routes.agent` | 必填 | 执行 route，模型需支持 `tools` 调用 |
| `plugins.xiaoqing_agent.ai.routes.vision` | 不配则读图不可用 | `read_image` 读本地图片用的 route，模型需要 `image` modality |
| `plugins.xiaoqing_agent.ai.routes.triage` | 复用 `agent` | “聊天还是做事”判断 route，建议快速、关闭思考的模型 |
| `plugins.xiaoqing_agent.inline_wait_seconds` | `12` | 当轮等待结果的秒数，超出后先回一句再后台交付 |
| `plugins.xiaoqing_agent.task_timeout_seconds` | `1800` | 单个任务总时长 |
| `plugins.xiaoqing_agent.max_steps` | `40` | 单个任务的模型轮数 |
| `plugins.xiaoqing_agent.max_admin_tasks` | `3` | Bot 管理员的任务同时运行数 |
| `plugins.xiaoqing_agent.max_member_tasks` | `4` | 其他人的任务合计同时运行数。`0` 表示只有 Bot 管理员能用 |
| `plugins.xiaoqing_agent.max_tasks_per_member` | `1` | 其他人每人同时运行的任务数 |

agent 使用发起人的命令权限。每条命令分别检查自身权限。Python、Shell 和文件读写跟随 `/shell`。SSH 跟随 `/ssh`。Codex 跟随 `/codex`。`plugin.json` 将这三条命令限定为 Bot 管理员使用。
全部字段、工具与数据目录见 [XiaoQing Agent](../plugins/xiaoqing_agent/README.md#️-开启)。

### XiaoQing Life

小青自己的网上生活。生活模拟默认关闭，观测站网站随插件加载启动：

| 路径 | 默认 | 说明 |
|---|---|---|
| `plugins.xiaoqing_life.enabled` | `false` | 总开关。关闭时定时任务不做事，`life.turn_context` 返回 `None`，聊天与没装时一致 |
| `plugins.xiaoqing_life.ai.routes.reasoning` | 必填 | 消化、研究、整理和睡前写日记用的 route；缺失时上网只记“模型未配置”并退避 |
| `plugins.xiaoqing_life.ai.routes.vision` | 无 | 梗图安全判定；缺失时不收梗图 |
| `plugins.xiaoqing_life.max_sessions_per_day` | `5` | 她的一天（配置时区，凌晨 5 点分界）最多上网次数 |
| `plugins.xiaoqing_life.max_llm_calls_per_day` | `30` | 同一天内所有模型调用合计，调用前先计数，失败不退还 |
| `plugins.xiaoqing_life.browse_when_busy` | `false` | 作息为“忙”时是否允许偷偷刷一下 |
| `plugins.xiaoqing_life.blocked_keywords` | `[]` | 在内置屏蔽词之外再丢弃的内容 |
| `plugins.xiaoqing_life.web_port` | `12002` | 观测站监听端口，只绑定 `127.0.0.1`，范围 `1..65535`；下次启动或重载插件时生效 |
| `plugins.xiaoqing_life.travel_enabled` | `true` | 每天请 Codex 画旅行明信片；`enabled` 关闭时不画 |

聊天一侧的取用开关在 `xiaoqing_chat` 行为配置的 `life.enable_online_life`、`life.online_life_timeout_seconds`、`life.enable_life_memes` 和 `life.enable_life_share`。
全部字段、命令与数据目录见 [XiaoQing Life](../plugins/xiaoqing_life/README.md#️-开启)，观测站的页面与旅行明信片见 [小青观测站](../plugins/xiaoqing_life/README.md#小青观测站)。

### XiaoQing Memory

小青的统一记忆，默认开启：

| 路径 | 默认 | 说明 |
|---|---|---|
| `plugins.xiaoqing_memory.enabled` | `true` | 总开关。关闭时 `memory.*` 服务返回 `None`，聊天、办事、网上生活照常工作，只是没有长期记忆 |
| `plugins.xiaoqing_memory.ai.routes.reasoning` | 无 | 夜间整理用的 route；缺失时只记录和取用，不整理 |
| `plugins.xiaoqing_memory.event_keep_days` | `365` | 原始记录保留天数（按整月删） |
| `plugins.xiaoqing_memory.night_start` / `night_end` | `"02:00"` / `"05:00"` | 夜间整理窗口（配置时区） |
| `plugins.xiaoqing_memory.backlog_trigger_events` | `150` | 积压这么多新记录时窗口外也整理 |
| `plugins.xiaoqing_memory.max_llm_calls_per_day` | `16` | 整理用的模型调用合计，调用前先计数，失败不退还 |

预算、上限和保留期的其余字段见 [XiaoQing Memory](../plugins/xiaoqing_memory/README.md#配置字段)。

### QingSSH

公开预算字段位于 `plugins.qingssh`：

| 字段组 | 示例字段 | 作用 |
|---|---|---|
| 命令 | `command_timeout_seconds=30` | 远端命令预算。`0` 交给管理员主动停止 |
| QQ action | `qq_max_actions=6` | 单次结果 action 数 |
| QQ 文本 | `qq_max_text_chars=10000`、`qq_max_message_chars=1800` | 累计文本与单消息预算 |
| 首尾预览 | `qq_head_chars=6000`、`qq_tail_chars=2000` | 长输出预览保留量 |
| 投递 | `qq_send_interval_seconds=0.35`、`qq_send_timeout_seconds=5` | action 间隔与单次等待预算 |
| 归档 | `archive_max_bytes=67108864`、`archive_tail_bytes=1048576`、`archive_retention_files=20` | 命令归档大小、尾部与数量预算 |

其余资料分别保存在下列位置，备份时需要一并覆盖：

| 资料 | 位置 | 管理方式 |
|---|---|---|
| 服务器索引（主机、端口、用户名、认证类型、私钥路径） | `data/qingssh/servers.json` | `/ssh add`、`/ssh remove` 等管理员命令 |
| 密码 | 插件密钥存储，服务器索引只记录 `password_ref` 引用 | `/ssh add` 写入，删除服务器时回收 |
| 私钥 | 服务器索引中 `key_path` 指向的本机文件 | 部署者自行管理 |
| Host Key | 当前系统用户的 `~/.ssh/known_hosts` | 首次连接确认后写入 |
| `~/.ssh/config` 中的主机与跳板设置 | 当前系统用户的 `~/.ssh/config` | `/ssh import`、`/sshconfig` 读取 |

`servers.json` 读取失败时插件会阻止后续写入，避免空快照覆盖原文件；修复文件后重新加载插件即可。
[QingSSH 配置](../plugins/qingssh/README.md#️-服务器配置) 提供连接 schema。

### Codex

公开作业策略位于 `plugins.codex`：

| 字段组 | 示例字段 | 作用 |
|---|---|---|
| 队列 | `max_parallel_jobs=2`、`per_session_queue_limit=10` | 全局并行与单会话等待任务 |
| 进程 | `spawn_timeout_seconds=30`、`job_timeout_seconds=3600` | CLI 启动与作业时限 |
| 输出 | `max_stdout_bytes`、`max_stderr_bytes`、`max_json_line_bytes`、`max_final_output_bytes` | stdout、stderr、JSONL 与最终结果预算 |
| QQ | `max_qq_text_chars=60000`、`max_qq_images=10` | 聊天文本与图片数量预算 |
| 制品扫描 | `artifact_scan_max_entries=5000`、`artifact_scan_max_depth=8` | 作业制品遍历预算 |
| 图片 | `max_image_artifacts`、`max_image_bytes`、`max_image_total_bytes`、`max_image_pixels`、`max_image_frames` | 图片数量、字节和解码预算 |
| 沙箱 | `sandbox="workspace-write"` | `read-only`、`workspace-write`、`danger-full-access` |
| 审批 | `approval_policy="never"` | `untrusted`、`on-failure`、`on-request`、`never` |
| 仓库 | `skip_git_repo_check=true` | Codex CLI Git 仓库检查开关 |
| 旅行明信片 | `travel_model=""` | `xiaoqing_life` 的旅行绘图任务使用的模型，留空时用 CLI 默认模型 |

工作目录、允许根目录、保留期、紧急磁盘阈值和 arXiv 摘要会话等扩展字段见 [Codex 配置](../plugins/codex/README.md#️-基础配置)。

### Shell 与 Smalltalk

| 路径 | 示例 | 说明 |
|---|---|---|
| `plugins.shell.terminal.backend` | `"direct"` | `direct` 通过 `create_subprocess_exec` 启动外部程序。`git-bash` 通过 Git Bash 执行命令文本 |
| `plugins.shell.terminal.executable` | Git Bash 绝对路径 | `git-bash` 后端的解释器路径 |
| `plugins.smalltalk.voice_probability` | `0` | 纯文本闲聊转语音概率，范围 `0..1` |

Shell 命令启用列表、工作目录与审计开关位于 `secrets.plugins.shell`。
[Shell 配置](../plugins/shell/README.md#️-终端配置) 说明两种后端。
[插件使用手册](09-plugins.md) 说明全部 34 个插件的配置入口与数据位置。

---

## ⚙️ Secrets

Core 顶层敏感字段：

| 字段 | 说明 |
|---|---|
| `inbound_token` | HTTP/WS Inbound Bearer token |
| `onebot_token` | 主动 WebSocket 与 HTTP Action Bearer token |
| `admin_user_ids` | Bot 管理员 QQ 列表 |
| `ai.providers.<name>.api_key` | AI Provider API Key |
| `plugins.<name>` | 插件私有凭据 |

`inbound_token` 在启用 Inbound Listener 时使用非空高熵字符串。
`onebot_token` 与 OneBot 实现保持一致。
空字符串对应双方约定的匿名模式。
`admin_user_ids` 使用正整数 QQ 列表。

示例 secrets 中的插件字段：

| 命名空间 | 字段 | 用途 |
|---|---|---|
| `plugins.flickr` | `api_key` | Flickr App Garden 公共只读 API Key |
| `plugins.wolframalpha` | `appid` | Wolfram\|Alpha App ID |
| `plugins.voice` | `subscription_key`、`region`、`voice_name`、`style`、`role`、`proxy` | Azure Speech 凭据、音色与代理 |
| `plugins.chat` | `token`、`bot_id`、`proxy` | Coze API 凭据与代理 |
| `plugins.twitter` | `user_id`、`headers.authorization`、`cookies`、`proxy`、`max_pages` | X/Twitter 抓取身份、会话和页数预算 |
| `plugins.signin.yingshijufeng` | `app_id`、`kdt_id`、`access_token`、`sid` | 影视飓风有赞签到凭据 |
| `plugins.ads_paper` | `ads_token` | SciX API Token（NASA ADS 的继任服务，原 ADS Token 继续有效） |

QingSSH、Shell 与 Codex 的管理员命令会维护各自 secret 命名空间。
字段 schema 位于对应插件 README。
生产 secrets 使用随机高熵 token，并通过文件权限、备份策略和主机访问控制保护。
反向代理可增加来源限制、速率限制与外层认证。

---

## ⚙️ 配置热重载

管理员命令：

```text
/reload
```

发布流程：

1. `ConfigManager` 连续读取稳定文件快照。
2. JSON、结构、范围、地址和敏感来源状态完成校验。
3. ConfigManager 生成新 revision。
4. 应用层串行发布管理员、凭据、插件视图、AI route、网络端点和调度参数。
5. 插件配置订阅者接收同一 revision。

### 生效矩阵

| 配置类别 | 生效方式 |
|---|---|
| Bot 名称、前缀、默认群、Session、并发、时区 | `/reload` 或配置 watcher |
| OneBot 地址、开关、队列与 token | 已确认 revision 发布后重建连接 |
| Inbound 地址、开关、worker、队列与 token | 已确认 revision 发布后由候选 Listener 切换代次 |
| `plugin_execution` 与插件 watcher 策略 | `/reload` 或配置 watcher |
| AI Provider、profile、route 与 API Key | 已确认 revision 发布后由下一次 AI 调用读取 |
| 插件公开配置与私有凭据 | 已确认 revision 发布后由插件读取 |
| 定时任务目标群（`plugins.<插件名>.schedules`） | 已确认 revision 发布后由下一次执行读取 |
| 有效的外部 `secrets.json` 单文件候选 | 管理员核对私聊字段摘要后执行 `/reload` |
| `data_root`、`logging`、`napcat_account`、`mkl_threading_layer` | 重启主进程或生产启动链 |
| 插件源码、Manifest 与 `watch_files` | `/reload`、插件 watcher 或 restart-only 环境的进程重启 |

运行中重新读取时，普通配置校验失败继续使用 last-known-good 快照；启动时的第一次读取没有可沿用的快照，见 [启动时读不到有效配置](#启动时读不到有效配置)。
敏感来源缺失、损坏、不可读或与新公开配置尚未确认配对时立即进入 fail-closed 状态，撤销网络凭据与运行时管理员视图。
管理员私聊中的 `/set_secret` 适用于已有 secret 路径，由 Core 完成持锁写入和 revision 发布。

配置 watcher 将分别保存的新文件代视为独立来源事件。
当前公开配置签名与已确认版本一致、当前来源对可信且外部 secrets 文件完整有效时，新文件代作为待确认候选保存。
运行时继续使用已确认 secrets，revision 与凭据代保持当前值。
Core 使用当前可信 OneBot 通道私聊全部当前管理员，通知包含文件有效状态及新增、删除、修改的字段路径，所有字段值保持隐藏。
`/reload` 会重新读取稳定磁盘来源并原子发布候选。

新公开配置可先发布。
与该来源尚未确认配对的 secrets 进入 `INCONSISTENT` 撤权状态。
主动 WebSocket 是唯一控制通道的部署采用“停止服务 → 原子保存两个完整文件 → 启动服务”，启动读取会确认新的来源对。
保留独立受保护 Inbound 的实例可在文件稳定后通过该通道执行 `/reload`。

单个配置文件上限为 8 MiB，JSON 树深度上限为 64，节点上限为 100000。
Core 拒绝非有限数字、重复不稳定读取和超出预算的来源。
部署脚本在停服窗口内先写临时文件并原子替换，再启动服务。

网络地址或 token 变化会唤醒主动 WebSocket 任务，并通过候选 Listener 提交 Inbound 新代。
同一时间只有已提交代接纳新事件。
候选 Listener 起不来（例如新端口已被占用）时，配置应用报出这个错误，旧 Listener 在原地址继续接纳事件，只认当前发布的 token：发布前的 token 不再有效，当前没有可信 token 时它也不接纳。
之后再执行 `/reload` 会重新尝试切换到配置声明的 Listener。

---

## ✅ 生产配置检查表

- `bot_name`、命令前缀和默认群符合目标 QQ 场景。
- `inbound_token` 使用高熵随机值。
- Inbound Listener 绑定 loopback 或受控代理网络。
- 反向代理负责 TLS、来源策略和速率限制。
- `admin_user_ids` 只包含当前管理员。
- AI Key 与插件凭据位于 secrets。
- 定时任务目标群、服务器地址等部署设置位于 config，仓库文件与发布版本一致。
- 有效的 secrets 单文件替换由管理员核对字段摘要并执行 `/reload`；公开配置变更与双来源替换安排在停服窗口内。
- `data_root`、日志目录和备份目录具有明确所有权。
- Windows 生产启动链下 `logging.console` 为 `false`。
- `plugin_execution` 为长任务插件配置合适预算。
- Pendo Web Cookie Secure 与 HTTPS 部署保持一致。
- 正式启动前完成 `python scripts/run_full_uat.py --plan-only` 和完整 UAT。

---

## 🧭 下一步

- 首次部署：[快速开始](01-getting-started.md)
- 插件字段：[插件使用手册](09-plugins.md)
- 网络与消息路径：[消息处理流程](08-message-flow.md)
