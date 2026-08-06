# 🔧 06 - 配置详解

本章集中说明 XiaoQing 的主要配置项，适合部署前后对照检查。

---

## 📁 配置文件

XiaoQing 使用两个 JSON 配置文件：

| 文件 | 用途 | 是否提交 Git |
|------|------|-------------|
| `config/config.json` | 基础配置 | ✅ 可以 |
| `config/secrets.json` | 敏感配置 | ❌ 不要 |

---

## config.json

### 完整示例

```json
{
  "bot_name": "小青",
  "command_prefixes": ["/"],
  "require_bot_name_in_group": true,
  "default_group_ids": [],
  
  "enable_ws_client": false,
  "enable_inbound_server": true,
  
  "onebot_ws_uri": "ws://127.0.0.1:11000/ws",
  "onebot_http_base": "http://127.0.0.1:11001",
  
  "inbound_ws_uri": "ws://127.0.0.1:12000/ws",
  "inbound_http_base": "http://127.0.0.1:12000",
  "inbound_trusted_tls_proxy": false,
  "ws_queue_size": 200,
  "inbound_ws_broadcast_timeout_seconds": 5.0,
  
  "max_concurrency": 5,
  "data_root": "data",
  "plugin_execution": {
    "timeout_seconds": 60,
    "parallel_limit": 4,
    "admission_queue_limit": 64,
    "sync_parallel_limit": 1,
    "sync_queue_limit": 16,
    "failure_threshold": 3,
    "cooldown_seconds": 60,
    "drain_timeout_seconds": 5,
    "global_sync_queue_limit": 256
  },
  "enable_plugin_watcher": false,
  "session_timeout": 300,
  "timezone": "Asia/Shanghai",
  
  "log_level": "INFO",
  "log_to_file": true,
  "log_to_console": true,
  "log_use_color": true,
  "log_max_size_mb": 10,
  "log_backup_count": 5,
  "log_rotation": "time",
  
  "plugins": {
    "smalltalk_provider": "smalltalk",
    "codex": {
      "protected_sessions": ["astro-ph"],
      "arxiv_summary": {
        "label": "astro-ph",
        "methodology": "arxiv-summary-methodology.md"
      },
      "max_parallel_jobs": 2,
      "per_session_queue_limit": 10,
      "session_ttl_days": 90,
      "artifact_retention_days": 30,
      "emergency_disk_bytes": 10737418240,
      "emergency_queue_limit": 1000,
      "spawn_timeout_seconds": 30,
      "job_timeout_seconds": 3600,
      "max_stdout_bytes": 16777216,
      "max_stderr_bytes": 4194304,
      "max_json_line_bytes": 1048576,
      "max_final_output_bytes": 8388608,
      "max_qq_text_chars": 60000,
      "artifact_scan_max_entries": 5000,
      "artifact_scan_max_depth": 8,
      "max_image_artifacts": 20,
      "max_image_bytes": 20971520,
      "max_image_total_bytes": 104857600,
      "max_image_pixels": 40000000,
      "max_image_frames": 120,
      "max_qq_images": 10,
      "sandbox": "workspace-write",
      "approval_policy": "never",
      "skip_git_repo_check": true
    }
  }
}
```

### 机器人配置

#### bot_name
- **类型**：`string`
- **默认**：`"小青"`
- **说明**：机器人名称。群聊中包含此名称的消息会被处理。

```json
{"bot_name": "小助手"}
```

#### command_prefixes
- **类型**：`string[]`
- **默认**：`["/"]`
- **说明**：命令前缀列表。消息以这些前缀开头时被视为命令。

```json
{"command_prefixes": ["/", "!", "。"]}
```

#### require_bot_name_in_group
- **类型**：`boolean`
- **默认**：`true`
- **说明**：群聊是否需要包含 bot_name 才处理。设为 `false` 则响应所有群消息。

```json
{"require_bot_name_in_group": false}
```

#### default_group_ids
- **类型**：`int[]`
- **默认**：`[]`
- **说明**：默认发送群列表，用于定时任务等。

```json
{"default_group_ids": [123456, 789012]}
```

### 通信配置

默认本地端口布局如下：

| 端口 | 服务 | 方向 | 配置项 |
|------|------|------|--------|
| `11000` | OneBot WebSocket | XiaoQing 主动连接 NapCat/OneBot | `onebot_ws_uri` |
| `11001` | OneBot HTTP API | XiaoQing 调用 NapCat/OneBot | `onebot_http_base` |
| `12000` | XiaoQing Inbound HTTP/WS | NapCat/OneBot 推送到 XiaoQing | `inbound_http_base` / `inbound_ws_uri` |
| `12001` | Pendo Web 控制台 | 浏览器访问 Pendo | `plugins.pendo.web_port` |

#### enable_ws_client
- **类型**：`boolean`
- **默认**：`false`
- **说明**：是否启用 WebSocket 客户端（主动连接 OneBot）。

#### enable_inbound_server
- **类型**：`boolean`
- **默认**：`true`
- **说明**：是否启用 Inbound 服务器（被动接收 OneBot 推送）。

#### napcat_account
- **类型**：`string`
- **默认**：`""`
- **说明**：Windows 生产监控器启动本机 NapCat 时追加的 QQ 账号参数。该值由 `scripts/run-bot-monitor.ps1` 从 `config/config.json` 读取，不写入启动脚本；留空或省略时不追加账号参数。

#### mkl_threading_layer
- **类型**：`string`
- **默认**：`""`
- **说明**：Windows 生产监控器为 Bot 子进程设置的可选 `MKL_THREADING_LAYER`。留空或省略时完整继承部署环境；例如使用混合 NumPy/MKL/PyTorch 安装且经验证需要 TBB 时可设为 `"TBB"`。监控器只在创建 Bot 日志泵进程树期间临时注入该值，随后恢复自身原环境，不会固定 Python/Conda 路径，也不会传给后续启动的 NapCat。修改后需要重启监控器。

#### onebot_ws_uri
- **类型**：`string`
- **默认**：`"ws://127.0.0.1:11000/ws"`
- **说明**：OneBot WebSocket 地址（enable_ws_client 时使用）。

#### onebot_http_base
- **类型**：`string`
- **默认**：`"http://127.0.0.1:11001"`
- **说明**：OneBot HTTP API 基础地址。

#### inbound_http_base
- **类型**：`string`
- **默认**：`"http://127.0.0.1:12000"`
- **说明**：Inbound HTTP 服务监听地址（提供 `/event`、`/health`、`/metrics`）。留空则不启动 HTTP Inbound。XiaoQing 当前只实现明文 `http://` listener；`https://` 会被配置校验拒绝，TLS 应在反向代理处终止。

#### inbound_ws_uri
- **类型**：`string`
- **默认**：`"ws://127.0.0.1:12000/ws"`
- **说明**：Inbound WebSocket 服务监听地址（仅 WS）。支持与 `inbound_http_base` 使用不同端口；留空则不启动 WS Inbound。XiaoQing 当前只实现 `ws://` listener；`wss://` 会被拒绝，WSS 应由 TLS 反向代理提供。

#### inbound_trusted_tls_proxy
- **类型**：`boolean`
- **默认**：`false`
- **说明**：非 loopback 明文 listener 的显式安全确认。默认情况下，Inbound 只能绑定 `localhost`、`127.0.0.0/8` 或 `::1`；绑定 `0.0.0.0`、`::`、局域网 IP 或普通主机名会直接拒绝启动。只有当 listener 位于受控网络中、外部访问必须经过可信 TLS 反向代理且防火墙已阻断直接明文访问时，才可设为 `true`。该开关不会为 XiaoQing 启用 TLS。

#### ws_queue_size
- **类型**：`int`
- **默认**：`200`
- **范围**：`1..10000`
- **说明**：Inbound HTTP/WS 统一 dispatcher 的等待上限，同时作为主动 OneBot WS Client 的接收缓冲上限。该配置始终有界，不接受 `0`；队列满时 Inbound HTTP 返回 `503`，Inbound WS 返回过载错误。

### 运行时配置

#### max_concurrency
- **类型**：`int`
- **默认**：`5`
- **说明**：最大并发处理消息数。这是**全局并发控制**的核心参数。

**工作原理**：
使用 `asyncio.Semaphore` 限制同时处理的消息数量。无论通过 WebSocket Client 还是 Inbound Server 接收消息，最终都会经过 `Dispatcher.handle_event()` 时获取此信号量。

**适用范围**：
- ✅ OneBot WebSocket Client（连接到 NapCatQQ）
- ✅ Inbound WebSocket Server（被动接收推送）
- ✅ Inbound HTTP Server

**调优建议**：
```json
// 低负载场景（个人使用）
{"max_concurrency": 5}

// 中等负载（多群组）
{"max_concurrency": 10}

// 高负载场景（大量群组）
{"max_concurrency": 20}
```

⚠️ **注意**：过高的并发数可能导致资源耗尽，建议根据服务器性能调整。

#### plugin_execution

插件入口统一经过 execution gate，同步 callback 和 `core.plugin_base.run_sync()` 还会经过按插件隔离的同步 bulkhead。四个共享 worker 按插件轮转提交：即使插件 A 堆积长任务，`sync_parallel_limit <= 3` 也至少为其它插件保留一个 worker。入口、插件同步队列和全局同步队列都有硬上限；满载时框架快速报告过载，不会把请求无限堆入内存。

字段采用严格类型校验，不接受字符串数字、布尔值冒充整数、未知键或显式 `null`。字段可省略以使用默认/继承值。

| 字段 | 默认值 | 有效范围 | 作用 |
|---|---:|---:|---|
| `timeout_seconds` | `60` | `0`，或 `0.1..86400` | 单次 callback 等待上限；`0` 禁用调用超时 |
| `parallel_limit` | `4` | `1..1024` | 单插件同时执行的入口数 |
| `admission_queue_limit` | `64` | `0..10000` | 超过并行入口后的等待数；`0` 表示不允许排队 |
| `sync_parallel_limit` | `1` | `1..3` | 单插件同时占用的共享同步 worker 数；上限 3 保留跨插件前进空间 |
| `sync_queue_limit` | `16` | `0..10000` | 单插件等待同步 worker 的任务数；`0` 表示不允许排队 |
| `failure_threshold` | `3` | `1..10000` | 连续失败多少次后开启熔断 |
| `cooldown_seconds` | `60` | `0.1..86400` | 熔断冷却时间 |
| `drain_timeout_seconds` | `5` | `0.1..3600` | reload/unload/shutdown 的有界等待时间 |
| `global_sync_queue_limit` | `256` | `1..100000` | 所有插件共享的同步等待硬上限；只能写在 `plugin_execution` 顶层 |

`overrides` 的键必须是仅含 ASCII 字母、数字和下划线的插件名；值只能包含插件级字段，不能嵌套 `overrides`，也不能设置全局的 `global_sync_queue_limit`。override 中省略的字段继承顶层值。

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
      "codex": {"timeout_seconds": 0},
      "qingssh": {"timeout_seconds": 0},
      "jupyter": {"timeout_seconds": 0},
      "shell": {"timeout_seconds": 0}
    }
  }
}
```

`drain_timeout_seconds` 不会强行终止 Python 线程。取消尚未启动的同步项会把它从队列删除；已经运行的线程会继续被真实 future 跟踪。若 drain 到期仍未返回，旧插件保留代码、状态和 broker 引用并进入关闭的 quarantine，绝不会同时安装新实例；日志会报告仍在运行及排队的 async/sync 数量。应用停机同样使用有界期限关闭接纳、清空队列并回收 broker，未结束的真实线程会明确报告为隔离工作。

#### enable_plugin_watcher
- **类型**：`boolean`
- **默认**：`false`
- **说明**：是否自动监控插件文件变化并热重载插件。

默认关闭，避免开发中的半成品文件在运行时被立即载入。开启后，框架会先运行模块导入
屏障的行为探针；通过后按 `plugin_poll_interval` 轮询插件目录并在发现变更时自动
reload。探针失败不会阻止机器人启动或首次加载插件，但 watcher 和手动插件 reload
都会关闭，日志会说明插件变更必须重启进程才能生效。

watcher 对每个插件路径和每轮扫描分别隔离可恢复的删除、改名、权限和原子替换竞态；一次失败只会受限记录并在下一轮重试，不会永久终止长期任务。应用还会监督 watcher，意外返回或异常退出后按有上限的指数退避启动唯一新代。当前运行数据位于外置 `data_root`，不会进入源码扫描；新发现插件会先完成旧数据目录的一次性迁移，再形成源码指纹，遍历只剪枝 `__pycache__/`。

指纹读取会核对打开前后文件身份，并在完整文件集读取结束后再次核对所有路径与身份。无法证明来自同一稳定快照时保留未改变授权的旧代并等待下一轮，不会发布混合文件快照；Manifest 授权本身变化但无法验证候选时则撤下旧授权代。

```json
{"enable_plugin_watcher": true}
```

#### plugin_poll_interval
- **类型**：`float`
- **默认**：`3600`
- **说明**：插件 watcher 轮询目录变化的间隔，单位秒。

```json
{"plugin_poll_interval": 2.0}
```

#### data_root
- **类型**：`string`
- **默认**：`"data"`
- **说明**：所有插件运行数据的项目级根目录。相对路径按项目根解析，绝对路径直接使用；目录必须位于 `plugins/` 源码树之外。修改后需要重启主进程。

每个插件只能通过 `context.data_dir` 使用自己的子目录，例如默认配置下 Pendo 为 `data/pendo/`。首次加载插件时，如果旧 `plugins/<name>/data/` 存在，Core 会先在新数据根内复制到私有临时目录并原子发布；随后把旧目录移到 `data_root/.legacy-plugin-data/<name>/`，避免它继续留在源码树，同时保留一份人工回退副本。跨盘迁移也先完成临时复制和原子发布才删除原位置；任一步失败都会拒绝加载且不覆盖现有目录。新目录是唯一运行时权威，Core 不会双读或把归档数据覆盖回来；同名归档已存在时会 fail closed，要求运维先核对，而不会静默覆盖。

```json
{"data_root": "D:/xiaoqing-runtime/plugins"}
```

#### inbound_ws_max_workers
- **类型**：`int`
- **默认**：`8`
- **范围**：`1..128`
- **说明**：Inbound HTTP 与 WebSocket 共享 dispatcher 的 worker 协程数量。字段名为兼容旧配置保留。

**仅对 Inbound Server 有效**，不影响主动 OneBot WS Client。

**工作原理**：
HTTP `/event` 与 WebSocket `/ws` 在鉴权、解析和归一化后进入同一个按会话键排序的有界调度器。同键跨传输严格 FIFO，不同键可以并行；排队事件会在 handler 执行前复验当前认证代。总接纳容量为 worker 数加 `ws_queue_size` 个等待项。

**建议配置**
```json
{"inbound_ws_max_workers": 8}  // 通常无需调整
```

worker 最终仍需经过 `Dispatcher` 的 `max_concurrency` 门禁。通常将 worker 数设为不高于或接近 `max_concurrency`；过多 worker 只会在下游门禁等待。

#### inbound_ws_broadcast_timeout_seconds
- **类型**：`float`
- **默认**：`5.0`
- **范围**：`0 < value <= 300`
- **说明**：向单个 Inbound WebSocket 客户端广播 Action 的最长等待时间（秒）。每个连接独立计时并并发发送；超时或发送失败的连接会从广播目标中移除并异步关闭。只有至少一个客户端确认写入成功时，本次 WebSocket 投递才算成功，否则继续尝试 OneBot HTTP 回退。

#### session_timeout
- **类型**：`int`
- **默认**：`300`
- **说明**：会话默认超时时间（秒）。

#### timezone
- **类型**：`string`
- **默认**：`"Asia/Shanghai"`
- **说明**：定时任务时区。

### 日志配置

#### log_level
- **类型**：`string`
- **默认**：`"INFO"`
- **可选值**：`DEBUG`, `INFO`, `WARNING`, `ERROR`
- **说明**：日志级别。

#### log_to_file
- **类型**：`boolean`
- **默认**：`true`
- **说明**：是否输出日志到文件。

#### log_to_console
- **类型**：`boolean`
- **默认**：`true`
- **说明**：是否输出日志到控制台。

#### log_use_color
- **类型**：`boolean`
- **默认**：`true`
- **说明**：控制台是否使用彩色输出。

#### log_max_size_mb
- **类型**：`int`
- **默认**：`10`
- **说明**：单个日志文件最大大小（MB）。

#### log_backup_count
- **类型**：`int`
- **默认**：`5`
- **说明**：保留的日志备份数量。

#### log_rotation
- **类型**：`string`
- **默认**：`"time"`
- **可选值**：`time`, `size`
- **说明**：日志滚动策略。

### 统一 AI/VLM 注册表

模型配置分成三个层次，避免每个插件重复维护 URL、模型名和密钥。

1. `ai.providers` 定义服务商连接信息，只放可公开的 API Base、接口路径和代理。
2. `ai.models` 定义可复用的模型 profile，关联 provider、真实模型名、模态和服务商特有默认参数。
3. `plugins.<插件>.ai.routes.<路由>.models` 定义该插件可用的有序模型链；第一个是主模型，后续项按顺序 fallback。

```json
{
  "ai": {
    "providers": {
      "deepseek": {
        "api_base": "https://api.deepseek.com",
        "endpoint_path": "/chat/completions",
        "proxy": ""
      },
      "zhipu": {
        "api_base": "https://open.bigmodel.cn/api/paas/v4",
        "endpoint_path": "/chat/completions",
        "proxy": ""
      }
    },
    "models": {
      "deepseek-flash": {
        "provider": "deepseek",
        "model": "deepseek-v4-flash",
        "modalities": ["text"],
        "request_defaults": {
          "thinking": {"type": "disabled"}
        }
      },
      "deepseek-flash-thinking": {
        "provider": "deepseek",
        "model": "deepseek-v4-flash",
        "modalities": ["text"],
        "request_defaults": {
          "thinking": {"type": "enabled"},
          "reasoning_effort": "high"
        }
      },
      "glm-5.2": {
        "provider": "zhipu",
        "model": "glm-5.2",
        "modalities": ["text"]
      },
      "glm-4.6v-flash": {
        "provider": "zhipu",
        "model": "glm-4.6v-flash",
        "modalities": ["text", "image"]
      }
    }
  },
  "plugins": {
    "pendo": {
      "ai": {
        "routes": {
          "parse": {
            "models": ["deepseek-flash", "glm-5.2"],
            "temperature": 0.3,
            "max_tokens": 1000,
            "timeout_seconds": 30,
            "total_timeout_seconds": 60,
            "max_retry": 1,
            "retry_interval_seconds": 1
          }
        }
      }
    }
  }
}
```

模型 profile 字段：

| 字段 | 类型 | 说明 |
|------|------|------|
| `provider` | `string` | 必须引用 `ai.providers` 中的名称 |
| `model` | `string` | 发给服务商的真实模型 ID |
| `modalities` | `string[]` | 至少包含 `text`；视觉模型还应包含 `image` |
| `request_defaults` | `object` | 模型特有的非保留请求参数，例如 `thinking` |

route 字段：

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `models` | `string[]` | 必需 | 有序模型 profile；第 0 项为主模型 |
| `temperature` | `number` | 未设置 | 路由默认采样温度，调用方可按任务覆盖 |
| `top_p` | `number` | 未设置 | 路由默认 `top_p` |
| `max_tokens` | `int` | 未设置 | 路由默认输出 token 上限 |
| `timeout_seconds` | `number` | `30` | 单次 HTTP 尝试超时 |
| `total_timeout_seconds` | `number` | `max(timeout_seconds, 60)` | 整条重试和 fallback 链总超时 |
| `max_retry` | `int` | `1` | 每个模型内部重试次数 |
| `retry_interval_seconds` | `number` | `0.5` | 指数退避的基础间隔 |
| `fallback_on` | `string[]` | 内置安全集合 | 允许切换到下一个模型的错误类别 |
| `request_defaults` | `object` | `{}` | 当前插件 route 的非保留请求参数 |

默认只在网络、超时、限流、服务端错误、模型不可用、无效响应和空响应上 fallback。认证失败、请求参数错误和请求体过大不会被后备模型掩盖。管理员显式使用 `/xc 模型 <别名>` 后，该会话会严格固定到选中的 profile；不做手动覆盖时才使用完整 route 链。

`model`、`messages`、`stream`、采样参数和工具字段属于保留键，不能藏在 `request_defaults` 中覆盖。每次调用只读取一份新的原子配置快照，因此 `/reload config` 后的新请求会使用新 route 和新凭据，正在执行的请求仍保持内部一致。

### 插件配置

#### plugins
- **类型**：`object`
- **说明**：插件全局配置。

```json
{
  "plugins": {
    "smalltalk_provider": "xiaoqing_chat",
    "smalltalk": {
      "voice_probability": 0.3
    }
  }
}
```

#### plugins.smalltalk_provider
- **类型**：`string`
- **默认**：`"smalltalk"`
- **可选值**：`"smalltalk"`, `"xiaoqing_chat"`, 或其他实现 `handle_smalltalk()` 的插件
- **说明**：闲聊提供者插件

**smalltalk**（默认）
- 基于规则的简单闲聊
- 无需额外配置
- 回复简单、固定

**xiaoqing_chat**（推荐）
- 基于 LLM 的智能对话
- 支持长期记忆、情绪系统、表情学习
- 启用媒体配置后可把图片消息写入上下文，主回复 LLM 也可通过出站 marker 带出本地图片、表情包或 QQ 表情
- 需要配置 LLM API（见下方 secrets.json）
- 智能回复频率控制

**配置示例**：
```json
{
  "plugins": {
    "smalltalk_provider": "xiaoqing_chat"
  }
}
```

**特性说明**：
- 当使用 `xiaoqing_chat` 时，所有消息会先进入 `observe_message()` 供插件更新上下文
- 只有通过 dispatcher 门控并落到 smalltalk 回落时，才会进入 `handle_smalltalk()`
- 由插件内部的 attention gate、硬频控、普通插话概率、PFC planner 和 reply checker 控制是否回复
- `/xc`、私聊、`@`、直接叫名字、只喊名字后的追问、reply 引用小青、以及有近期上下文锚点的“她/ta”共指召唤会走 forced 路径
- 支持向量数据库长期记忆

#### plugins.smalltalk.voice_probability

- **类型**：`number`
- **默认**：`0.2`
- **范围**：`0` 到 `1`（含端点）
- **说明**：仅控制 `smalltalk` 基础插件把纯文本回复交给 `voice.synthesize_text` 的概率；显式的布尔值、
  字符串、NaN、无穷或越界数字会安全禁用语音。混合媒体、超过 3000 字符、provider 不可用或合成失败时
  保留原回复。

#### plugins.codex
- **类型**：`object`
- **说明**：Codex 后台会话队列插件配置。配置放在 `config.json -> plugins.codex`；如需覆盖 `codex_bin` 等本机私有路径，也可以放在 `secrets.json -> plugins.codex`。

```json
{
  "plugins": {
    "codex": {
      "protected_sessions": ["astro-ph"],
      "arxiv_summary": {
        "label": "astro-ph",
        "methodology": "arxiv-summary-methodology.md"
      },
      "max_parallel_jobs": 2,
      "per_session_queue_limit": 10,
      "session_ttl_days": 90,
      "artifact_retention_days": 30,
      "emergency_disk_bytes": 10737418240,
      "emergency_queue_limit": 1000,
      "spawn_timeout_seconds": 30,
      "job_timeout_seconds": 3600,
      "max_stdout_bytes": 16777216,
      "max_stderr_bytes": 4194304,
      "max_json_line_bytes": 1048576,
      "max_final_output_bytes": 8388608,
      "max_qq_text_chars": 60000,
      "artifact_scan_max_entries": 5000,
      "artifact_scan_max_depth": 8,
      "max_image_artifacts": 20,
      "max_image_bytes": 20971520,
      "max_image_total_bytes": 104857600,
      "max_image_pixels": 40000000,
      "max_image_frames": 120,
      "max_qq_images": 10,
      "sandbox": "workspace-write",
      "approval_policy": "never",
      "skip_git_repo_check": true
    }
  }
}
```

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `codex_bin` | `string` | `"codex"` | Codex CLI 可执行文件名或路径 |
| `default_cwd` | `string` | `<context.data_dir>/workspaces` | `/codex create <name>` 未指定 `cwd:` 时使用的工作目录；不存在时会自动创建 |
| `allowed_cwd_roots` | `string[]` | `[default_cwd]` | 允许创建 Codex 会话的目录根；实际工作目录必须在这些根目录下 |
| `max_parallel_jobs` | `int` | `2` | 全局最多同时运行的 Codex CLI 任务数 |
| `per_session_queue_limit` | `int` | `10` | 每个 Codex 标签允许排队的非内部任务数（范围 1-1,000）；达到后硬拒绝新任务 |
| `session_ttl_days` | `int` | `90` | 空闲非受保护会话的自动归档天数；`0` 关闭 |
| `artifact_retention_days` | `int` | `30` | 已结束 job、输出、隔离快照和删除归档的保留天数；`0` 关闭 |
| `emergency_disk_bytes` | `int` | `10737418240` | Codex 数据目录紧急磁盘阈值，最小 64 MiB；达到后拒绝新任务 |
| `emergency_queue_limit` | `int` | `1000` | 进程保护紧急队列上限（范围 10-10,000，且不低于会话上限） |
| `spawn_timeout_seconds` | `int` | `30` | 创建 Codex CLI 子进程的最长等待秒数（范围 1-120）；取消会等待 spawn handoff 收敛 |
| `job_timeout_seconds` | `int` | `3600` | 单个 Codex 任务超时秒数 |
| `max_stdout_bytes` | `int` | `16777216`（16 MiB） | JSON stdout 累计预算，范围 64 KiB-128 MiB；超限终止任务的整棵进程树 |
| `max_stderr_bytes` | `int` | `4194304`（4 MiB） | stderr 累计预算，范围 64 KiB-64 MiB；超限终止任务的整棵进程树 |
| `max_json_line_bytes` | `int` | `1048576`（1 MiB） | 单条 stdout JSON 事件预算，范围 16 KiB-8 MiB；超限立即终止任务 |
| `max_final_output_bytes` | `int` | `8388608`（8 MiB） | 最终输出文件预算，范围 64 KiB-64 MiB；超限终止任务，只归档有界的头尾截断副本 |
| `max_qq_text_chars` | `int` | `60000` | QQ 文本字符预算，范围 2,000-200,000；超限时完整结果归档，只投递截断文本和归档位置 |
| `artifact_scan_max_entries` | `int` | `5000` | 单任务最多扫描的制品目录条目数，范围 10-20,000；到达上限即停止继续遍历 |
| `artifact_scan_max_depth` | `int` | `8` | 制品目录最大扫描深度，范围 1-16；更深条目不进入收集流程 |
| `max_image_artifacts` | `int` | `20` | 单任务最多接受的图片数，范围 1-100；超出数量的候选被拒绝 |
| `max_image_bytes` | `int` | `20971520`（20 MiB） | 单张图片字节预算，范围 64 KiB-100 MiB；超限产物被拒绝 |
| `max_image_total_bytes` | `int` | `104857600`（100 MiB） | 单任务已接受图片总字节预算，范围 64 KiB-512 MiB；超限的后续产物被拒绝 |
| `max_image_pixels` | `int` | `40000000` | 单张图片真实解码像素预算，范围 1,024-100,000,000；超限或签名/解码失败的产物被拒绝 |
| `max_image_frames` | `int` | `120` | 单张多帧图片帧数预算，范围 1-500；超限产物被拒绝 |
| `max_qq_images` | `int` | `10` | 每个任务最多向 QQ 发送的已接受图片数，范围 1-20；其余已归档图片不发送 |
| `sandbox` | `string` | `"workspace-write"` | 传给 Codex CLI 的 sandbox 模式 |
| `approval_policy` | `string` | `"never"` | 传给 Codex CLI 的审批策略 |
| `skip_git_repo_check` | `boolean` | `true` | 是否给 `codex exec` 添加 `--skip-git-repo-check` |
| `protected_sessions` | `string[]` | `[arxiv_summary.label]` | 受保护会话列表；删除这些会话必须同时使用 `--force --protected` |
| `arxiv_summary.label` | `string` | `"astro-ph"` | arXiv Filter 自动摘要使用的固定 Codex 会话名 |
| `arxiv_summary.cwd` | `string` | `default_cwd` | arXiv 摘要会话的工作目录；应位于 `allowed_cwd_roots` 下 |
| `arxiv_summary.methodology` | `string` | `"arxiv-summary-methodology.md"` | 摘要 prompt 要求 Codex 在工作目录中读取的方法论文件名 |

四项输出字节预算是强制进程级限制，触发后任务会以输出超限结束；其中最终输出文件超限只归档有界头尾副本，QQ 文本字符超限则采用“完整结果写入任务归档、QQ 截断并附归档位置”；制品扫描和图片数量/字节/签名/解码/像素/帧数超限时，相关候选会被跳过或拒绝，并记录拒绝原因。`max_qq_images` 只约束发送，已通过校验的归档仍保留。配置值超出表中范围时会被钳制到最近边界。

Codex 是可信管理员级高权限插件：全部 `/codex` 命令保持 `admin_only: true`，仅 `admin_user_ids` 中的 Bot 管理员可用。上述预算只保护 Bot 存活性与 QQ 投递链路，不限制管理员配置 `sandbox`、`approval_policy` 或允许工作目录的灵活性。

路径输入建议统一使用 `/` 斜杠。Windows 上可以写 `C:/workspace/project`，插件会按运行系统解析；Linux/macOS 上仍写 `/srv/xiaoqing/workspaces/project`。如果 bot 运行在非 Windows 系统，Windows 盘符路径会被拒绝。

arXiv Filter 会把筛选出的 positive 论文链接投递给 `arxiv_summary.label` 指定的 Codex 会话。首次没有 Codex thread 时，会先投递一条静默初始化任务；之后每次摘要 prompt 都会明确要求读取 `arxiv_summary.methodology`，但不会把该文件正文拼进 prompt。该文件需要提前放在 `arxiv_summary.cwd` 中。历史摘要与在途任务按“arXiv 源列表日期 + 规范化论文链接集合”匹配，而不是只按本地日期匹配。

Codex 插件会把运行时状态写入 `data/codex/`：`sessions.json` 保存会话标签和 thread id，`session/<label>/conversation.jsonl` 保存每个标签的用户任务、Codex 回复、取消、删除事件和图片记录，`session/<label>/images/` 保存已透传到 QQ 的图片副本，`session/<label>/jobs/` 保存单次任务的 artifacts 目录，`deleted_sessions/` 保存删除会话时归档的旧历史。该目录不应提交到 Git。

`cancel` 和 `stop` 是同一个操作：取消排队任务，或终止正在运行的 Codex CLI 子进程。能否保留已完成的中间文件取决于 Codex CLI 和任务自身行为。

---

## secrets.json

### 完整示例

```json
{
  "onebot_token": "",
  "inbound_token": "your-secret-token",
  "admin_user_ids": [123456789, 987654321],
  "ai": {
    "providers": {
      "deepseek": {
        "api_key": "your-deepseek-api-key"
      },
      "zhipu": {
        "api_key": "your-zhipu-api-key"
      }
    }
  },
  "plugins": {
    "shell": {
      "whitelist": ["ls", "pwd", "echo"]
    },
    "ads_paper": {
      "ads_token": "your-ads-api-token"
    }
  }
}
```

### 认证配置

#### onebot_token
- **类型**：`string`
- **默认**：`""`
- **说明**：OneBot HTTP 与主动 WebSocket 共用的认证 Token。来源状态为 VALID 时，字段缺省或明确设为 `""` 表示操作者允许匿名 OneBot；其他字符串通过 HTTP Bearer 头以及主动 WebSocket 的认证 header 发送。布尔、数字、null、数组或对象不是合法 token，会撤销出站访问而不是转换为字符串。
- **失败关闭**：主动 WebSocket 会根据已安装 `websockets` 的公开签名选择新版 `additional_headers` 或旧版 `extra_headers`；参数必须明确支持关键字调用。配置非空 token 却无法证明任一参数可用时拒绝启动。`secrets.json` 为 MISSING、INVALID、UNAVAILABLE、INCONSISTENT 时，即使内存中的 token 为空，也表示凭据来源不可信：HTTP 不发请求、WebSocket 不调用 `connect`；文件恢复为稳定 VALID revision 后才重新启用。
- **热更新**：地址、token 或凭据可信状态变化会立即撤销旧 holder，并唤醒当前退避/连接 attempt。正常和异常短连接共用带连续抖动的 5–60 秒指数退避，稳定 30 秒才复位。

#### inbound_token
- **类型**：`string`
- **默认**：`""`
- **说明**：Inbound 服务器的认证 Token。OneBot 推送时需要携带此 Token；只要配置了 `inbound_http_base` 或 `inbound_ws_uri`，首次启动就要求非空字符串。默认空值只适用于关闭 Inbound 或未配置任何 listener 的状态，不表示匿名开放。
- **失败关闭**：仅接受来源状态为 VALID 的精确字符串。`secrets.json` 来源异常，或值为布尔、数字、null、数组、对象及字符串子类时，运行态 token 会清空，已有 WebSocket 会话被撤销，当前 listener 与尚未发布的候选 listener 都切换到全拒绝状态。

### 权限配置

#### admin_user_ids
- **类型**：`int[]`
- **默认**：`[]`
- **说明**：管理员 QQ 号列表。仅从 VALID secrets 视图加载；来源异常时运行态管理员集合立即清空，旧权限不会作为 last-known-good 保留。

```json
{"admin_user_ids": [123456789]}
```

### 统一 AI/VLM 凭据

`config.json` 中 `ai.providers` 的同名 provider，只在 `secrets.json` 保存密钥：

```json
{
  "ai": {
    "providers": {
      "deepseek": {"api_key": "your-deepseek-api-key"},
      "zhipu": {"api_key": "your-zhipu-api-key"}
    }
  }
}
```

provider 名必须与 `config.ai.providers` 完全一致。API Base、接口路径、代理和模型 ID 不属于凭据，应放在 `config.json`。插件拿不到这里的密钥；它只能通过 `context.capabilities.ai` 调用自己的命名 route。这样既避免复制配置，也防止一个插件读取另一个插件使用的 provider 凭据。

从旧版插件私有配置迁移时，先备份 `config/secrets.json`，再把旧的 provider
连接信息手工移动到项目级 `ai.providers`。如果同一 provider 存在不同密钥，
请先确认实际使用值，不要自动合并；与模型无关的插件私有字段继续保留在原位置。

### 插件私有配置

#### plugins
- **类型**：`object`
- **说明**：各插件的私有配置。

插件只能访问自己的私有命名空间：
```python
api_key = context.get_secret("api_key")
```

统一 AI provider 凭据不属于插件私有命名空间，不能用 `context.get_secret()` 读取。

Minecraft 的服务器 profile 是非敏感连接配置与敏感凭据分离的示范：
`plugins/minecraft/config.json` 只保存 `host`、`port`、`log_file` 等公开字段，
RCON 密码必须保存为 `config/secrets.json -> plugins.minecraft.<配置名>` 的字符串。
插件目录配置中出现 `password` 会被拒绝；密码不通过聊天参数传递。

#### xiaoqing_chat 模型路由

普通聊天、科学/数值回复、独立复核和视觉模型都在公开配置中引用统一 profile：

```json
{
  "plugins": {
    "xiaoqing_chat": {
      "ai": {
        "default_model_alias": "deepseek",
        "model_aliases": {
          "deepseek": "deepseek-flash",
          "glm": "glm-5.2"
        },
        "routes": {
          "chat": {
            "models": ["deepseek-flash", "glm-5.2"],
            "timeout_seconds": 15,
            "total_timeout_seconds": 60,
            "max_retry": 2,
            "retry_interval_seconds": 0.5
          },
          "checker": {
            "models": ["deepseek-flash-thinking", "deepseek-pro", "glm-5.2"],
            "timeout_seconds": 8,
            "total_timeout_seconds": 20,
            "max_retry": 0
          },
          "reasoning": {
            "models": ["deepseek-flash-thinking", "deepseek-pro", "glm-5.2"],
            "timeout_seconds": 15,
            "total_timeout_seconds": 45,
            "max_retry": 1,
            "retry_interval_seconds": 0.5
          },
          "vision": {
            "models": [
              "glm-4v-flash",
              "glm-4.6v-flash",
              "glm-4.6v",
              "glm-4.1v-thinking-flash"
            ],
            "timeout_seconds": 20,
            "total_timeout_seconds": 70,
            "max_retry": 1,
            "retry_interval_seconds": 0.5
          }
        }
      }
    }
  }
}
```

`model_aliases` 只用于 `/xc 模型` 的易读短名称，值必须引用 `chat.models` 中的 profile。`/xc 模型 默认` 清除手动覆盖并恢复自动路由；`/xc 模型 <别名>` 严格固定主回复 profile。未固定时，普通闲聊走关闭思考的 `chat`，数字、单位和科学关系走开启思考的 Flash `reasoning`，回复复核同样使用开启思考的 Flash 且不继承主回复固定项，图片走 `vision`。思考 token 与最终答案共用 `max_tokens`，科学主回复至少使用 2048，checker 使用 1024。DeepSeek 当前只提供有效的 `high` 和 `max` 两档思考强度，`low`/`medium` 会映射到 `high`；需要降低延迟时应优先使用 Flash，并只在不需要事实推理的路径关闭思考。

远程 reply checker 只使用一个短调用预算。它超时、不可用或返回无效协议时，在确定性检查通过后放行当前候选；确定性重复、刷屏、媒体错位及无依据人物经历仍会硬拒绝。这样既保留质量门禁，也不会因 checker 故障重复生成直至触发插件熔断。

#### xiaoqing_chat 运行时配置（`plugins/xiaoqing_chat/config/xiaoqing_config.json`）

`xiaoqing_chat` 的 planner、深度对话和媒体行为开关走插件自己的 `config/xiaoqing_config.json`。这里不再包含 provider、模型名或接口路径：

```json
{
  "planner": {
    "enable_planner": true,
    "think_mode": "dynamic"
  },
  "brain_chat": {
    "enable_private_brain_chat": false,
    "private_planner_always_on": true,
    "brain_think_level": 2
  },
  "media": {
    "enable_inbound_media_context": true,
    "max_media_per_message": 1
  }
}
```

常用 planner / 深度对话开关：

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|---------|------|
| `planner.enable_planner` | `boolean` | `true` | 是否启用 PFC/planner；关闭后走直接回复链 |
| `planner.think_mode` | `string` | `"dynamic"` | 思考等级。`dynamic` 会按近期历史长度自动映射到 0/1/2，也可直接写 `"0"` / `"1"` / `"2"` |
| `brain_chat.enable_private_brain_chat` | `boolean` | `false` | 是否启用私聊深度对话模式 |
| `brain_chat.private_planner_always_on` | `boolean` | `true` | 深度对话模式下是否始终启用 planner |
| `brain_chat.brain_think_level` | `int` | `2` | 深度对话模式的固定 think level |

常用媒体行为开关：

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|---------|------|
| `enable_inbound_media_context` | `boolean` | `true` | 是否把用户图片渲染成 `[图片：...]` / `[表情包：...]` 写入对话上下文 |
| `max_media_per_message` | `int` | `1` | marker 协议每条回复最多解析并发送一个出站媒体 |

视觉模型 profile 和 route 示例：

```json
{
  "ai": {
    "models": {
      "glm-4.6v-flash": {
        "provider": "zhipu",
        "model": "glm-4.6v-flash",
        "modalities": ["text", "image"],
        "request_defaults": {
          "thinking": {"type": "disabled"}
        }
      }
    }
  },
  "plugins": {
    "xiaoqing_chat": {
      "ai": {
        "routes": {
          "vision": {
            "models": ["glm-4.6v-flash", "glm-4.6v"]
          }
        }
      }
    }
  }
}
```

同一份 `secrets.ai.providers.zhipu.api_key` 可供 GLM 文本和视觉 profile 复用。GLM provider 应使用标准按量 API Base；Coding Plan 专属端点不适用于这里。

**缓存与索引位置**：

- 收到的图片：`data/xiaoqing_chat/media/inbox/`
- 可发送图片：`data/xiaoqing_chat/media/reply_images/`
- 可发送表情包：`data/xiaoqing_chat/media/library/`
- 图片描述缓存：`data/xiaoqing_chat/media/render_cache.json`
- 媒体注册索引：`data/xiaoqing_chat/media/index.json`
- 表情包索引：`data/xiaoqing_chat/media/library/index.json`

媒体目录固定在该插件的 `context.data_dir/media/` 下（默认 `data/xiaoqing_chat/media/`），不通过 `xiaoqing_config.json` 配置。插件会把入站图片统一落到 `media/inbox/`。如果图片被识别成表情包，就会自动复制进 `media/library/` 并写入索引，后续可被主回复 LLM 通过 `[想发表情:hint]` marker 复用。新收进图库的表情包也会让这条消息更倾向于触发一次自然回应。

出站阶段不会因为发现旧图库条目缺少 `description` / `marker` / `emotion_tags` 就同步重跑视觉模型。当轮回复会先按主 LLM 输出的 `[想发图片:hint]` / `[想发表情:hint]` / `[想发QQ表情:hint]` 查找候选，坏条目的补修会放到后台异步执行，不阻塞这次回复。

NapCat/OneBot 的纯 `mface` 消息如果没有直接携带图片源，插件会尝试通过 `onebot_http_base` 对应的 HTTP API 调用 `get_msg` 和 `get_image` 回收真实图片；拿不到真实图片时，再退回成仅保留摘要的 `[表情包：...]` 标记。

QQ 原生 `face` 表情不会走图片下载链，而是直接转换成 `[QQ表情：微笑]` 这类 marker 进入上下文；如果拿不到名称，则退回成 `[QQ表情：id=14]`。

如果视觉模型未配置、配置不完整，或请求失败，插件会退回到基于文件名/摘要的保守标记，不会阻断普通文本对话；同时会在日志里打出 `media.analyze.skip` / `media.analyze.fail`，方便定位是“没拿到图”还是“视觉模型没跑起来”。

后续补齐 `xiaoqing_chat.ai.routes.vision` 及其模型 profile 后，旧的低质量 fallback 图片缓存会在再次命中时自动重跑识图并覆盖，不需要手工清空整个 `render_cache.json`。

统一层面向 OpenAI Chat Completions 兼容接口。新增服务商时，把公开连接信息放到 `config.ai.providers`，模型 ID 和模态放到 `config.ai.models`，只把 API Key 放到 `secrets.ai.providers`；插件 route 无需复制这些字段。

#### pendo 配置

pendo 有两部分配置：**AI 解析 route**（日程智能解析需要）和**用户偏好**（通过 `/pendo settings` 命令设置）。route 写在 `config.json`：

```json
{
  "plugins": {
    "pendo": {
      "ai": {
        "routes": {
          "parse": {
            "models": ["deepseek-flash", "glm-5.2"],
            "temperature": 0.3,
            "max_tokens": 1000
          }
        }
      }
    }
  }
}
```

密钥来自对应 profile 引用的 `secrets.ai.providers`。route 不可用或请求失败时，日程添加会回退到本地规则解析，其他功能不受影响；用户关闭 `ai_consent` 时，不会把受保护正文发送给远程模型。

**Web 控制台认证**：

pendo Web UI 使用 JWT Token 认证，无需手动配置密码。Token 由以下方式管理：

```
# 获取一次性登录码（私聊发送，7 天内仅可兑换一次）
/pendo web token

# 获取 Scriptable 小组件只读 Token（默认 365 天）
/pendo web widget-token

# 吊销当前用户全部尚未过期的 Widget Token
/pendo web widget-revoke

# Token 签名密钥优先级：
# 1. 环境变量 PENDO_WEB_TOKEN_SECRET
# 2. data/pendo/web_token_secret.txt（首次运行自动生成）
```

Scriptable 脚本只配置 `BASE_URL`；首次在 App 内运行时把私聊收到的 Widget Token 存入 iOS Keychain，不要把 Token 写进脚本常量。

**Web 服务运行配置**：

```json
{
  "plugins": {
    "pendo": {
      "web_enabled": true,
      "web_host": "127.0.0.1",
      "web_port": 12001,
      "web_session_cookie_secure": false,
      "web_demo_enabled": false
    }
  }
}
```

`web_enabled`、`web_session_cookie_secure` 和 `web_demo_enabled` 必须是 JSON 布尔值，`web_host` 必须是非空字符串，`web_port` 必须是 `1..65535` 的整数。非法的新配置会被拒绝并保留上一代有效设置。保存有效配置后，Pendo 根据配置修订号原子切换整组运行参数；开关、监听地址或端口变化时会相应停止或重启 Web 服务。

Windows 上遇到 `WinError 10013` 时，常见原因是系统拒绝绑定端口。此时优先修改 `plugins.pendo.web_port`，例如改为 `12003`。

用户偏好（时区、简报时间、日记提醒等）通过 `/pendo settings` 命令在运行时修改，存储于数据库，无需修改配置文件。

公开 demo 会话默认关闭。可通过 `plugins.pendo.web_demo_enabled` 临时开启；生产环境建议保持关闭。Pendo Web 运行参数没有第二套环境变量来源。

#### qingssh 配置

```json
{
  "plugins": {
    "qingssh": {
      "max_connections": 5,
      "command_timeout_seconds": 30,
      "qq_max_actions": 6,
      "qq_max_text_chars": 10000,
      "qq_max_message_chars": 1800,
      "qq_head_chars": 6000,
      "qq_tail_chars": 2000,
      "qq_send_interval_seconds": 0.35,
      "qq_send_timeout_seconds": 5,
      "archive_max_bytes": 67108864,
      "archive_tail_bytes": 1048576,
      "archive_retention_files": 20
    }
  }
}
```

**配置项说明**：

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|---------|------|
| `max_connections` | `int` | `32` | 最大并发连接数 |
| `command_timeout_seconds` | `number` | `30` | 单条远端命令时限；`0` 表示可信管理员显式关闭时限 |
| `qq_max_actions` | `int` | `6` | 单条命令最多尝试的 QQ 输出/状态 action 数 |
| `qq_max_text_chars` | `int` | `10000` | 单条命令 QQ 投影累计字符硬上限 |
| `qq_max_message_chars` | `int` | `1800` | 每个 QQ action 的文字上限 |
| `qq_head_chars` | `int` | `6000` | QQ 开头摘要预算 |
| `qq_tail_chars` | `int` | `2000` | QQ 末尾摘要候选预算 |
| `qq_send_interval_seconds` | `number` | `0.35` | QQ action 最小发送间隔 |
| `qq_send_timeout_seconds` | `number` | `5` | 单次 OneBot 发送最长等待时间 |
| `archive_max_bytes` | `int` | `67108864` | 本地输出归档硬上限；超出后只保留首尾并明确标记 |
| `archive_tail_bytes` | `int` | `1048576` | 归档硬上限触发后保留的末尾预算 |
| `archive_retention_files` | `int` | `20` | 最多保留的已提交命令输出归档数 |

补充说明：
- QingSSH 严格校验 `~/.ssh/known_hosts` 中的 Host Key；未知主机或 Host Key 变更不会自动放行。
- 从 `~/.ssh/config` 导入时，支持 `ProxyJump` 以及安全的 `ssh -W` 跳板形式；其他会在本地执行命令的 `ProxyCommand` 会被拒绝。
- 以上预算只约束 QQ 投影和 Bot 本地归档，不修改管理员提交的远端命令；QQ 截断时完整输出路径会随最终状态返回。

#### shell 配置

Shell 的公开终端配置放在 `config.json -> plugins.shell`。未配置时使用 `direct`，直接用 `create_subprocess_exec()` 启动程序；Windows 部署也可显式选择 Git Bash：

```json
{
  "plugins": {
    "shell": {
      "terminal": {
        "backend": "git-bash",
        "executable": "C:/Program Files/Git/bin/bash.exe"
      }
    }
  }
}
```

Git Bash 以 `--noprofile --norc -c` 运行，不加载用户启动脚本。可执行文件路径由部署者提供，项目不会扫描 Git、Conda 或虚拟环境目录；配置缺失或失效会明确报错，不会回退到另一个 Bash。

命令启用列表和超时放在 `secrets.json -> plugins.shell`：

```json
{
  "plugins": {
    "shell": {
      "whitelist": ["ls", "pwd", "git", "cp", "cmd", "robocopy"],
      "whitelist_mode": "extend",
      "timeout": 30,
      "disable_whitelist": false
    }
  }
}
```

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `whitelist` | `string[]` | `[]` | 自定义命令白名单 |
| `whitelist_mode` | `string` | `"replace"` | `replace` 表示只使用自定义白名单；`extend` 表示在默认白名单上追加 |
| `timeout` | `int` | `30` | 命令执行超时秒数 |
| `disable_whitelist` | `boolean` | `false` | 关闭白名单，危险模式；危险正则仍会生效 |

启用列表只表示管理员允许尝试的首入口，不表示终端中已经安装对应程序；`/shell list` 会按当前配置的终端区分可执行和未找到的入口。

路径参数会按 bot 所在系统归一化。QQ 里可以统一输入 `/` 斜杠路径，例如 Windows 的 `C:/workspace/a.txt` 或 Linux/macOS 的 `/srv/xiaoqing/workspaces/a.txt`。URL 不会被当作路径改写，`/c`、`/Y` 这类 Windows 选项也不会被误判为路径。

Windows 的 `copy`、`del`、`type` 等命令是 shell 内建命令，不能直接 `/shell copy ...`。需要复制文件时，优先用外部命令 `cp`、`xcopy`、`robocopy`，或者显式执行 `cmd /c copy <src> <dst>`。
其中 `cp` 只有在当前终端确实提供时才可用。`direct` 使用 Bot PATH；Git Bash 使用其自身命令环境。

#### ads_paper 配置

```json
{
  "plugins": {
    "ads_paper": {
      "ads_token": "your-ads-api-token"
    }
  }
}
```

`ads_token` 是 NASA ADS 搜索凭据。AI 摘要不再在这里复制 LLM 字段，而是使用 `config.plugins.ads_paper.ai.routes.summary`；当前示例链为 `deepseek-pro` → `glm-5.2`。route 不可用时，插件仍可返回 ADS 原始摘要。

---

## 配置实践

### 1. 开发环境
```json
// config.json
{
  "log_level": "DEBUG",
  "plugins": {
    "smalltalk_provider": "smalltalk"
  }
}

// secrets.json
{
  "onebot_token": "",
  "inbound_token": "dev-token"
}
```

### 2. 生产环境

```json
// config.json
{
  "log_level": "INFO",
  "max_concurrency": 10,
  "ai": {
    "providers": {
      "deepseek": {
        "api_base": "https://api.deepseek.com",
        "endpoint_path": "/chat/completions"
      },
      "zhipu": {
        "api_base": "https://open.bigmodel.cn/api/paas/v4",
        "endpoint_path": "/chat/completions"
      }
    },
    "models": {
      "deepseek-flash": {
        "provider": "deepseek",
        "model": "deepseek-v4-flash",
        "modalities": ["text"]
      },
      "glm-5.2": {
        "provider": "zhipu",
        "model": "glm-5.2",
        "modalities": ["text"]
      }
    }
  },
  "plugins": {
    "smalltalk_provider": "xiaoqing_chat",
    "xiaoqing_chat": {
      "ai": {
        "routes": {
          "chat": {
            "models": ["deepseek-flash", "glm-5.2"]
          }
        }
      }
    }
  }
}

// secrets.json
{
  "inbound_token": "strong-random-token-here",
  "admin_user_ids": [123456789],
  "ai": {
    "providers": {
      "deepseek": {
        "api_key": "your-deepseek-api-key"
      },
      "zhipu": {
        "api_key": "your-zhipu-api-key"
      }
    }
  }
}
```

### 3. 推荐配置（xiaoqing_chat）

从 `config/config.json.example` 复制完整的文本和视觉 profile。聊天 route 建议把低延迟模型放在第 0 项，把不同 provider 的稳定文本模型放在后面；视觉 route 同理：

```json
{
  "plugins": {
    "xiaoqing_chat": {
      "ai": {
        "default_model_alias": "deepseek",
        "model_aliases": {
          "deepseek": "deepseek-flash",
          "glm": "glm-5.2"
        },
        "routes": {
          "chat": {
            "models": ["deepseek-flash", "glm-5.2"]
          },
          "vision": {
            "models": ["glm-4v-flash", "glm-4.6v-flash", "glm-4.6v"]
          }
        }
      }
    }
  }
}
```

### 4. 单模型或本地兼容服务

不需要 fallback 时，route 的 `models` 只写一个 profile。对于 Ollama 等本地 OpenAI-compatible 服务，也沿用相同结构；密钥字段仍需提供服务端接受的非空值：

```json
// config.json
{
  "ai": {
    "providers": {
      "local": {
        "api_base": "http://127.0.0.1:11434/v1",
        "endpoint_path": "/chat/completions"
      }
    },
    "models": {
      "local-chat": {
        "provider": "local",
        "model": "your-local-model",
        "modalities": ["text"]
      }
    }
  },
  "plugins": {
    "xiaoqing_chat": {
      "ai": {
        "routes": {
          "chat": {"models": ["local-chat"]}
        }
      }
    }
  }
}

// secrets.json
{
  "ai": {
    "providers": {
      "local": {"api_key": "local"}
    }
  }
}
```

### 5. 仅本地测试
```json
// config.json
{
  "enable_ws_client": false,
  "enable_inbound_server": true,
  "inbound_http_base": "http://127.0.0.1:12000",
  "inbound_ws_uri": "",
  "log_level": "DEBUG"
}

// secrets.json
{
  "inbound_token": "dev-token"
}
```

### 6. 公网部署

XiaoQing 不直接终止 TLS。推荐让同机 Nginx/Caddy 监听公网 HTTPS/WSS，而 XiaoQing 继续只监听 loopback：

```json
// config.json
{
  "inbound_http_base": "http://127.0.0.1:12000",
  "inbound_ws_uri": "ws://127.0.0.1:12000/ws",
  "inbound_trusted_tls_proxy": false,
  "log_level": "INFO"
}

// secrets.json
{
  "inbound_token": "strong-random-token-here",
  "onebot_token": "your-onebot-token",
  "admin_user_ids": [123456789]
}
```

反向代理对公网提供 `https://` / `wss://`，再转发到上述 loopback 地址。不要把 `inbound_http_base` 写成 `https://...` 或把 `inbound_ws_uri` 写成 `wss://...`：这两个字段描述 XiaoQing 自己的本地 listener，而它没有证书/TLS 配置，安全校验会拒绝这种伪 TLS 配置。

代理至少要保留 Bearer 鉴权头，并为 WebSocket 转发 Upgrade：

```nginx
location / {
    proxy_pass http://127.0.0.1:12000;
    proxy_set_header Authorization $http_authorization;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
}
```

公网侧必须只开放代理的 HTTPS/WSS 端口；XiaoQing 的 12000 端口应由主机防火墙、容器 network policy 或安全组阻止客户端直连。

若反向代理与 XiaoQing 位于不同容器或不同受控主机，确实必须监听 `0.0.0.0`/局域网 IP 时，才设置 `"inbound_trusted_tls_proxy": true`；同时必须用容器网络规则或主机防火墙确保客户端无法绕过代理直接访问明文端口。

⚠️ **公网部署必须设置强随机 `inbound_token`，并强制所有外部流量经过 TLS 代理。**

⚠️ **`inbound_trusted_tls_proxy` 只是部署责任确认，不会自动配置证书、加密或访问控制。**

---

## 环境变量

XiaoQing 的主配置入口是 JSON 文件。部署环境需要把配置映射到环境变量时，可以在 `config.py` 中增加读取层。

```python
import os

class ConfigManager:
    def _load(self, path):
        data = ...  # 加载 JSON
        
        # 从环境变量覆盖
        if os.environ.get("XIAOQING_LOG_LEVEL"):
            data["log_level"] = os.environ["XIAOQING_LOG_LEVEL"]
        
        return data
```

---

## 配置热重载

配置文件支持热重载。默认启动后会自动监控 `config.json` 和 `secrets.json`；插件文件 watcher 默认关闭，需要显式启用 `enable_plugin_watcher`。手动命令适合需要立刻触发重载的场景：

```
/reload config
```

或在代码中：
```python
context.reload_config()
```

**注意**：
- 常用运行时配置都支持热重载；`enable_ws_client`、`onebot_ws_uri`、`enable_inbound_server`、`inbound_http_base`、`inbound_ws_uri`、`inbound_trusted_tls_proxy`、`ws_queue_size`、`inbound_ws_max_workers`、`inbound_ws_broadcast_timeout_seconds`、`max_concurrency`、`session_timeout`、`timezone` 等修改后，可通过 watcher 或 `/reload config` 生效。端口不冲突时会先把候选 listener 绑定为不接纳状态，完整排空旧入站 dispatcher 后再统一提交新代；候选预绑定失败不会影响旧服务。必须复用同一端口时会先排空旧代，并在候选失败后恢复旧 listener。
- 配置 watcher 默认开启；插件 watcher 只有在 `enable_plugin_watcher=true` 且模块导入屏障行为探针通过时才会自动 reload 插件；不满足能力时进入 restart-only 模式。插件运行数据在源码树外，遍历阶段只剪枝 `__pycache__/`；单路径文件竞态只影响本轮并在下一轮重试，watcher 任务意外退出后由应用按有界退避监督重启。
- `config.json` 缺失、暂时不可读或解析失败时保留最后一次有效普通配置并记录来源状态；`secrets.json` 出现同类问题时不会保留旧凭据，而是立即清空运行态 secrets（fail closed）。
- watcher 不能证明两个普通 JSON 文件的外部写入属于同一事务，也无法知道插件自定义配置中的哪些字段会改变凭据目标。因此，任何新的外部 `config.json` 或 `secrets.json` 文件代际都会应用普通配置但先让 secrets 进入 `INCONSISTENT` 状态并保持撤权；确认两个文件都已完整保存且没有其他进程继续写入后，必须执行 `/reload config` 才会成对授权该快照。即使原子替换后的字节与旧文件完全相同，也会按新的文件身份重新确认，避免删除后重建旧凭据时静默复活。
- 手动 reload 要求连续三次读取到相同的 config/secrets 内容，拦截常见的截断写入和分阶段保存；其线性化点是最后一次稳定读取。不要在执行 reload 的同时用不遵守项目锁的外部进程继续替换、删除或原地写文件；任何有限次文件读取都无法与这种非合作并发写入形成跨平台原子事务，之后发生的变更会由下一轮 watcher 检测并撤权。
- 单个配置源最大 8 MiB，递归树最多 100,000 个值、最大深度 64；超限、非有限浮点数和非 JSON 数据都会被拒绝。建议先写临时文件并原子替换，保存完成后再执行 `/reload config`。

---

## 日志文件

日志输出到 `logs/` 目录：

| 文件 | 内容 |
|------|------|
| `xiaoqing.log` | 所有日志 |
| `xiaoqing_error.log` | 错误日志 |
| `xiaoqing.log.2026-01-15` | 按日期滚动的备份 |

查看实时日志：
```bash
tail -f logs/xiaoqing.log
```

---

## ➡️ 下一步

- 高级主题 → [07-advanced.md](07-advanced.md)

---

## pendo Web 控制台部署

### 直接访问（默认）

```text
/pendo web start           # 启动，默认 127.0.0.1:12001
```

如需改端口，把 `config/config.json` 中的 `plugins.pendo.web_port` 改为合法整数；保存后配置热重载会重启监听端点。

访问 `http://127.0.0.1:12001`（或你自定义的新端口），使用 `/pendo web token` 私聊获得一次性登录码并粘贴登录。登录码 7 天内有效且仅可兑换一次；浏览器随后使用短期 HttpOnly session cookie，不会把 bearer 写入 localStorage。

默认 loopback HTTP 为本机开发保留非 Secure cookie。若把 `plugins.pendo.web_host` 改为非 loopback 地址，必须放在 HTTPS 反向代理之后，并同时配置 `"web_session_cookie_secure": true`；否则服务拒绝启动。

Web 控制台包含总览、日程、待办、账本、笔记、日记、搜索、统计、设置和迁移页面。迁移页面负责 `.pendo.zip` Bundle 的预览、导出、导入、冲突策略和审计日志；聊天端 `/pendo export` 只导出 Markdown 档案。

Pendo Web 的公开 demo 会话默认关闭。如需临时开启受控演示环境，把 `plugins.pendo.web_demo_enabled` 配置为 `true`。

### nginx 子路径反向代理

pendo Web 部署在子路径 `/pendo/` 时，可以通过 nginx 做前缀转发。当前前端静态资源和 API 请求都使用相对路径，因此一个带尾部 `/` 的 `proxy_pass` 可以同时覆盖页面与 `/api/*`。

```nginx
# nginx.conf 片段
location = /pendo {
    return 301 /pendo/;
}

location /pendo/ {
    proxy_pass http://127.0.0.1:12001/;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

**注意事项**
- `proxy_pass http://127.0.0.1:12001/;` 会把 `/pendo/...` 转成后端根路径 `/...`，因此浏览器里的 `/pendo/api/...` 会自动映射到后端的 `/api/...`
- pendo 后端真实 API 前缀是 `/api/`，`/pendo/api/` 由代理路径映射产生
- 环境变量 `PENDO_WEB_TOKEN_SECRET` 可用于在多实例/重启场景下保持 Token 签名密钥稳定

---

## 消息分发相关配置

框架使用 dispatcher 线性流程处理消息，无需额外配置即可启用。

**相关配置**：
- `bot_name`：影响 `has_prefix`、`has_bot_name` 与只喊名字回应
- `command_prefixes`：影响 `has_command_prefix` 与命令匹配
- `session_timeout`：影响活跃会话的 Step D 处理
- `plugins.smalltalk_provider`：影响 Step G 的 smalltalk 回落

**分发顺序**（固定，不可配置）：
1. 处理门控（Step A；先 resolve 再按类别 observe）
2. URL-only → url_parser（Step B；门控与静音之后，静音时跳过）
3. 只喊名字回应（Step C）
4. 活跃会话（Step D）
5. 命令匹配（Step E）
6. 未知命令提示（Step F）
7. smalltalk 回落（Step G）
