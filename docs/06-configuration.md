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
  
  "max_concurrency": 5,
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
      "default_cwd": "C:/Users/testuser/Desktop/XiaoQing/XiaoQing_Codex",
      "allowed_cwd_roots": ["C:/Users/testuser/Desktop/XiaoQing/XiaoQing_Codex"],
      "protected_sessions": ["astro-ph"],
      "arxiv_summary": {
        "label": "astro-ph",
        "cwd": "C:/Users/testuser/Desktop/XiaoQing/XiaoQing_Codex",
        "methodology": "arxiv-summary-methodology.md"
      },
      "max_parallel_jobs": 2,
      "per_session_queue_limit": 10,
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

#### random_reply_rate
- **类型**：`float`
- **默认**：`0.05`
- **范围**：`0.0` - `1.0`
- **状态**：兼容保留字段
- **说明**：该字段不参与 dispatcher 分发。群聊回复概率由 `xiaoqing_chat` 等 smalltalk provider 内部控制。配置项保留用于兼容已有配置，无副作用。

```json
{"random_reply_rate": 0.1}
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
| `12001` | Pendo Web 控制台 | 浏览器访问 Pendo | `PendoConfig.WEB_PORT` / `PENDO_WEB_PORT` |

#### enable_ws_client
- **类型**：`boolean`
- **默认**：`false`
- **说明**：是否启用 WebSocket 客户端（主动连接 OneBot）。

#### enable_inbound_server
- **类型**：`boolean`
- **默认**：`true`
- **说明**：是否启用 Inbound 服务器（被动接收 OneBot 推送）。

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
- **说明**：WebSocket 入队缓冲上限（同时作用于 Inbound WS Server 与 OneBot WS Client）。设为 `0` 表示不限制。

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

#### enable_plugin_watcher
- **类型**：`boolean`
- **默认**：`false`
- **说明**：是否自动监控插件文件变化并热重载插件。

默认关闭，避免开发中的半成品文件在运行时被立即载入。开启后，框架会按 `plugin_poll_interval` 轮询插件目录并在发现变更时自动 reload。

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

#### inbound_ws_max_workers
- **类型**：`int`
- **默认**：`8`
- **说明**：Inbound WebSocket Server 的 worker 协程数量。

**仅对 Inbound Server 有效**，不影响 OneBot WS Client。

**工作原理**：
当 Inbound Server 通过 WebSocket 接收到消息后，会放入内部队列，由多个 worker 协程并行从队列中取出并处理。

**建议配置**
```json
{"inbound_ws_max_workers": 8}  // 通常无需调整
```

建议 `inbound_ws_max_workers >= max_concurrency`，否则 worker 会等待信号量而造成浪费。

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
- `random_reply_rate` 不参与 dispatcher 分发
- 由插件内部的 attention gate、硬频控、普通插话概率、PFC planner 和 reply checker 控制是否回复
- `/xc`、私聊、`@`、直接叫名字、只喊名字后的追问、reply 引用小青、以及有近期上下文锚点的“她/ta”共指召唤会走 forced 路径
- 支持向量数据库长期记忆

#### plugins.codex
- **类型**：`object`
- **说明**：Codex 后台会话队列插件配置。配置放在 `config.json -> plugins.codex`；如需覆盖 `codex_bin` 等本机私有路径，也可以放在 `secrets.json -> plugins.codex`。

```json
{
  "plugins": {
    "codex": {
      "default_cwd": "C:/Users/testuser/Desktop/XiaoQing/XiaoQing_Codex",
      "allowed_cwd_roots": ["C:/Users/testuser/Desktop/XiaoQing/XiaoQing_Codex"],
      "protected_sessions": ["astro-ph"],
      "arxiv_summary": {
        "label": "astro-ph",
        "cwd": "C:/Users/testuser/Desktop/XiaoQing/XiaoQing_Codex",
        "methodology": "arxiv-summary-methodology.md"
      },
      "max_parallel_jobs": 2,
      "per_session_queue_limit": 10,
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
| `default_cwd` | `string` | `C:/Users/testuser/Desktop/XiaoQing/XiaoQing_Codex` | `/codex create <name>` 未指定 `cwd:` 时使用的工作目录；不存在时会自动创建 |
| `allowed_cwd_roots` | `string[]` | `[default_cwd]` | 允许创建 Codex 会话的目录根；实际工作目录必须在这些根目录下 |
| `max_parallel_jobs` | `int` | `2` | 全局最多同时运行的 Codex CLI 任务数 |
| `per_session_queue_limit` | `int` | `10` | 每个 Codex 标签允许排队的任务数 |
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

路径输入建议统一使用 `/` 斜杠。Windows 上可以写 `C:/Users/testuser/Desktop/project`，插件会按运行系统解析；Linux/macOS 上仍写 `/home/user/project`。如果 bot 运行在非 Windows 系统，Windows 盘符路径会被拒绝。

arXiv Filter 会把筛选出的 positive 论文链接投递给 `arxiv_summary.label` 指定的 Codex 会话。首次没有 Codex thread 时，会先投递一条静默初始化任务；之后每次摘要 prompt 都会明确要求读取 `arxiv_summary.methodology`，但不会把该文件正文拼进 prompt。该文件需要提前放在 `arxiv_summary.cwd` 中。

Codex 插件会把运行时状态写入 `plugins/codex/data/`：`sessions.json` 保存会话标签和 thread id，`session/<label>/conversation.jsonl` 保存每个标签的用户任务、Codex 回复、取消、删除事件和图片记录，`session/<label>/images/` 保存已透传到 QQ 的图片副本，`session/<label>/jobs/` 保存单次任务的 artifacts 目录，`deleted_sessions/` 保存删除会话时归档的旧历史。该目录不应提交到 Git。

`cancel` 和 `stop` 是同一个操作：取消排队任务，或终止正在运行的 Codex CLI 子进程。能否保留已完成的中间文件取决于 Codex CLI 和任务自身行为。

---

## secrets.json

### 完整示例

```json
{
  "onebot_token": "",
  "inbound_token": "your-secret-token",
  "admin_user_ids": [123456789, 987654321],
  "plugins": {
    "weather": {
      "api_key": "your-weather-api-key"
    },
    "shell": {
      "whitelist": ["ls", "pwd", "echo"]
    },
    "openai": {
      "api_key": "sk-xxx",
      "api_base": "https://api.openai.com/v1"
    }
  }
}
```

### 认证配置

#### onebot_token
- **类型**：`string`
- **默认**：`""`
- **说明**：连接 OneBot 的认证 Token。

#### inbound_token
- **类型**：`string`
- **默认**：`""`
- **说明**：Inbound 服务器的认证 Token。OneBot 推送时需要携带此 Token。

### 权限配置

#### admin_user_ids
- **类型**：`int[]`
- **默认**：`[]`
- **说明**：管理员 QQ 号列表。

```json
{"admin_user_ids": [123456789]}
```

### 插件私有配置

#### plugins
- **类型**：`object`
- **说明**：各插件的私有配置。

在插件中访问：
```python
plugin_cfg = context.secrets.get("plugins", {}).get("myplugin", {})
api_key = plugin_cfg.get("api_key")
```

#### xiaoqing_chat 配置

```json
{
  "plugins": {
    "xiaoqing_chat": {
      "default": "deepseek",
      "providers": {
        "deepseek": {
          "api_base": "https://api.deepseek.com",
          "api_key": "sk-your-chat-api-key",
          "model": "deepseek-chat",
          "endpoint_path": "/v1/chat/completions",
          "proxy": ""
        }
      }
    }
  }
}
```

**配置项说明**：

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|---------|------|
| `default` | `string` | `""` | 默认聊天 provider 名称；`/xc 模型 默认` 会切回这里 |
| `providers` | `object` | 必需 | 聊天模型提供商集合，键是 provider 名 |
| `providers.<name>.api_base` | `string` | 必需 | 聊天模型 API Base |
| `providers.<name>.api_key` | `string` | 必需 | 聊天模型 API Key |
| `providers.<name>.model` | `string` | 必需 | 聊天模型名称 |
| `providers.<name>.endpoint_path` | `string` | `"/v1/chat/completions"` | OpenAI 兼容接口路径 |
| `providers.<name>.proxy` | `string` | `""` | 可选代理 |

> [!TIP]
> `xiaoqing_chat` 使用 provider 结构读取 secrets。这样可以配置多套聊天模型，并通过 `/xc 模型 <名称>` 在运行时切换。

#### xiaoqing_chat 运行时配置（`plugins/xiaoqing_chat/config/xiaoqing_config.json`）

`xiaoqing_chat` 的 planner、深度对话和媒体行为开关走插件自己的 `config/xiaoqing_config.json`。视觉模型的 API Base / Key / Model 不再放这里，统一走项目级 `config/secrets.json`：

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
    "max_media_per_message": 1,
    "vision_provider": ""
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
| `vision_provider` | `string` | `""` | 可选。指定 `secrets.json -> plugins.xiaoqing_chat.vision.providers` 里的视觉 provider 名称；为空时优先使用 `vision.default` |

视觉 provider 示例：

```json
{
  "plugins": {
    "xiaoqing_chat": {
      "default": "deepseek",
      "providers": {
        "deepseek": {
          "api_base": "https://api.deepseek.com",
          "api_key": "sk-xxx",
          "model": "deepseek-chat",
          "endpoint_path": "/v1/chat/completions"
        }
      },
      "vision": {
        "default": "glm-4.6v-flash",
        "providers": {
          "glm-4.6v-flash": {
            "api_base": "https://open.bigmodel.cn/api/paas/v4",
            "api_key": "your-vision-key",
            "model": "glm-4.6v-flash",
            "endpoint_path": "/chat/completions",
            "thinking": {
              "type": "disabled"
            }
          }
        }
      }
    }
  }
}
```

**缓存与索引位置**：

- 收到的图片：`plugins/xiaoqing_chat/data/media/inbox/`
- 可发送图片：`plugins/xiaoqing_chat/data/media/reply_images/`
- 可发送表情包：`plugins/xiaoqing_chat/data/media/library/`
- 图片描述缓存：`plugins/xiaoqing_chat/data/media/render_cache.json`
- 媒体注册索引：`plugins/xiaoqing_chat/data/media/index.json`
- 表情包索引：`plugins/xiaoqing_chat/data/media/library/index.json`

媒体目录固定在 `plugins/xiaoqing_chat/data/media/` 下，不通过 `xiaoqing_config.json` 配置。插件会把入站图片统一落到 `data/media/inbox/`。如果图片被识别成表情包，就会自动复制进 `data/media/library/` 并写入索引，后续可被主回复 LLM 通过 `[想发表情:hint]` marker 复用。新收进图库的表情包也会让这条消息更倾向于触发一次自然回应。

出站阶段不会因为发现旧图库条目缺少 `description` / `marker` / `emotion_tags` 就同步重跑视觉模型。当轮回复会先按主 LLM 输出的 `[想发图片:hint]` / `[想发表情:hint]` / `[想发QQ表情:hint]` 查找候选，坏条目的补修会放到后台异步执行，不阻塞这次回复。

NapCat/OneBot 的纯 `mface` 消息如果没有直接携带图片源，插件会尝试通过 `onebot_http_base` 对应的 HTTP API 调用 `get_msg` 和 `get_image` 回收真实图片；拿不到真实图片时，再退回成仅保留摘要的 `[表情包：...]` 标记。

QQ 原生 `face` 表情不会走图片下载链，而是直接转换成 `[QQ表情：微笑]` 这类 marker 进入上下文；如果拿不到名称，则退回成 `[QQ表情：id=14]`。

如果视觉模型未配置、配置不完整，或请求失败，插件会退回到基于文件名/摘要的保守标记，不会阻断普通文本对话；同时会在日志里打出 `media.analyze.skip` / `media.analyze.fail`，方便定位是“没拿到图”还是“视觉模型没跑起来”。

后续补上显式视觉配置后，例如 `vision.default`，或者 `media.vision_provider` 指向单独的视觉 provider，旧的低质量 fallback 图片缓存会在再次命中时自动重跑识图并覆盖，不需要手工清空整个 `render_cache.json`。

**支持的 API 提供商**：

```json
// OpenAI
{
  "api_base": "https://api.openai.com/v1",
  "model": "gpt-4o-mini"
}

// Azure OpenAI
{
  "api_base": "https://your-resource.openai.azure.com/openai/deployments/your-deployment",
  "api_key": "your-azure-key"
}

// 兼容 OpenAI 的服务（如 DeepSeek、Qwen）
{
  "api_base": "https://api.deepseek.com/v1",
  "model": "deepseek-chat"
}
```

#### pendo 配置

pendo 有两部分配置：**AI 解析**（日程智能解析需要）和**用户偏好**（通过 `/pendo settings` 命令设置）。

AI 解析配置需要在 `secrets.json` 中填写：

```json
{
  "plugins": {
    "pendo": {
      "api_base": "https://api.openai.com/v1",
      "api_key": "sk-your-api-key",
      "model": "gpt-4o-mini"
    }
  }
}
```

| 配置项 | 类型 | 说明 |
|--------|------|------|
| `api_base` | `string` | LLM API 基础 URL（用于日程 AI 解析） |
| `api_key` | `string` | LLM API Key |
| `model` | `string` | 使用的 LLM 模型 |

**注意**：不配置 LLM 时，日程添加会回退到规则解析，其他功能不受影响。

**Web 控制台认证**：

pendo Web UI 使用 JWT Token 认证，无需手动配置密码。Token 由以下方式管理：

```
# 获取登录 Token（私聊发送，有效期 24 小时）
/pendo web token

# 获取 Scriptable 小组件只读 Token（默认长期有效）
/pendo web widget-token

# Token 签名密钥优先级：
# 1. 环境变量 PENDO_WEB_TOKEN_SECRET
# 2. plugins/pendo/data/web_token_secret.txt（首次运行自动生成）
```

**Web 服务监听地址**：

```text
# PowerShell
$env:PENDO_WEB_HOST="127.0.0.1"
$env:PENDO_WEB_PORT="12001"

# bash
PENDO_WEB_HOST=127.0.0.1
PENDO_WEB_PORT=12001
```

Windows 上遇到 `WinError 10013` 时，常见原因是系统拒绝绑定端口。此时优先换一个端口，例如 `PENDO_WEB_PORT=12003`。

用户偏好（时区、简报时间、日记提醒等）通过 `/pendo settings` 命令在运行时修改，存储于数据库，无需修改配置文件。

公开 demo 会话默认关闭。可通过全局配置 `plugins.pendo.web_demo_enabled` 或环境变量 `PENDO_WEB_DEMO_ENABLED=1` 临时开启；生产环境建议保持关闭。

#### qingssh 配置

```json
{
  "plugins": {
    "qingssh": {
      "max_connections": 5,
      "connection_timeout": 30,
      "command_timeout": 60,
      "auto_disconnect": true
    }
  }
}
```

**配置项说明**：

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|---------|------|
| `max_connections` | `int` | `5` | 最大并发连接数 |
| `connection_timeout` | `int` | `30` | 连接超时（秒） |
| `command_timeout` | `int` | `60` | 命令执行超时（秒） |
| `auto_disconnect` | `boolean` | `true` | 是否自动断开空闲连接 |

补充说明：
- QingSSH 严格校验 `~/.ssh/known_hosts` 中的 Host Key；未知主机或 Host Key 变更不会自动放行。
- 从 `~/.ssh/config` 导入时，支持 `ProxyJump` 以及安全的 `ssh -W` 跳板形式；其他会在本地执行命令的 `ProxyCommand` 会被拒绝。

#### shell 配置

Shell 插件的配置放在 `secrets.json -> plugins.shell`。它默认只允许白名单命令，并且直接用 `create_subprocess_exec()` 启动进程，不经过系统 shell。

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

路径参数会按 bot 所在系统归一化。QQ 里可以统一输入 `/` 斜杠路径，例如 Windows 的 `C:/Users/testuser/Desktop/a.txt` 或 Linux/macOS 的 `/home/user/a.txt`。URL 不会被当作路径改写，`/c`、`/Y` 这类 Windows 选项也不会被误判为路径。

Windows 的 `copy`、`del`、`type` 等命令是 shell 内建命令，不能直接 `/shell copy ...`。需要复制文件时，优先用外部命令 `cp`、`xcopy`、`robocopy`，或者显式执行 `cmd /c copy <src> <dst>`。

#### ads_paper 配置

```json
{
  "plugins": {
    "ads_paper": {
      "api_key": "your-ads-api-token",
      "max_results": 20,
      "default_format": "bibtex"
    }
  }
}
```

**配置项说明**：

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|---------|------|
| `api_key` | `string` | 必需 | NASA ADS API Token |
| `max_results` | `int` | `20` | 每次搜索最大结果数 |
| `default_format` | `string` | `"bibtex"` | 默认导出格式 |

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
  "plugins": {
    "smalltalk_provider": "xiaoqing_chat"
  }
}

// secrets.json
{
  "inbound_token": "strong-random-token-here",
  "admin_user_ids": [123456789],
  "plugins": {
    "xiaoqing_chat": {
      "default": "deepseek",
      "providers": {
        "deepseek": {
          "api_base": "https://api.deepseek.com",
          "api_key": "your-production-api-key",
          "model": "deepseek-chat",
          "endpoint_path": "/v1/chat/completions"
        }
      },
      "vision": {
        "default": "glm-4.6v-flash",
        "providers": {
          "glm-4.6v-flash": {
            "api_base": "https://open.bigmodel.cn/api/paas/v4",
            "api_key": "your-vision-api-key",
            "model": "glm-4.6v-flash",
            "endpoint_path": "/chat/completions",
            "thinking": {
              "type": "disabled"
            }
          }
        }
      }
    }
  }
}
```

### 3. 推荐配置（xiaoqing_chat）
```json
// config.json
{
  "bot_name": "小青",
  "command_prefixes": ["/"],
  "plugins": {
    "smalltalk_provider": "xiaoqing_chat"
  }
}

// secrets.json
{
  "plugins": {
    "xiaoqing_chat": {
      "default": "deepseek",
      "providers": {
        "deepseek": {
          "api_base": "https://api.deepseek.com",
          "api_key": "sk-your-chat-api-key",
          "model": "deepseek-chat",
          "endpoint_path": "/v1/chat/completions"
        }
      },
      "vision": {
        "default": "glm-4.6v-flash",
        "providers": {
          "glm-4.6v-flash": {
            "api_base": "https://open.bigmodel.cn/api/paas/v4",
            "api_key": "your-vision-api-key",
            "model": "glm-4.6v-flash",
            "endpoint_path": "/chat/completions",
            "thinking": {
              "type": "disabled"
            }
          }
        }
      }
    }
  }
}
```

### 4. 低成本配置（使用本地/免费 LLM）
```json
// secrets.json
{
  "plugins": {
    "xiaoqing_chat": {
      "default": "deepseek",
      "providers": {
        "deepseek": {
          "api_base": "https://api.deepseek.com",
          "api_key": "your-api-key",
          "model": "deepseek-chat",
          "endpoint_path": "/v1/chat/completions"
        }
      }
    }
  }
}
```

**推荐免费/低成本选项**：
- **DeepSeek**：`https://api.deepseek.com` - 免费额度较大
- **Qwen**：`https://dashscope.aliyuncs.com/compatible-mode/v1` - 通义千问
- **Ollama 本地部署**：完全免费，需要本地 GPU

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
- 常用运行时配置都支持热重载；`enable_ws_client`、`onebot_ws_uri`、`enable_inbound_server`、`inbound_http_base`、`inbound_ws_uri`、`inbound_trusted_tls_proxy`、`ws_queue_size`、`inbound_ws_max_workers`、`max_concurrency`、`session_timeout`、`timezone` 等修改后，可通过 watcher 或 `/reload config` 直接生效。listener 或 TLS proxy 声明变化会先停止旧 InboundManager，再按新策略重新 bind。
- 配置 watcher 默认开启；插件 watcher 只有在 `enable_plugin_watcher=true` 时才会自动 reload 插件，且会忽略 `plugins/*/data/` 下的运行时状态文件。
- 如果 `config.json` 或 `secrets.json` 一次写坏，运行时会保留最后一次有效快照；修复文件后重新 `/reload config` 即可。
- 自动 watcher 能发现文件变化。配置刚修改完成且需要立即生效时，仍建议手动执行一次 `/reload`。

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

# 如需改端口，在启动主进程前设置环境变量
# PowerShell
$env:PENDO_WEB_PORT="12003"
python main.py

# bash
PENDO_WEB_PORT=12003 python main.py
```

访问 `http://127.0.0.1:12001`（或你自定义的新端口），使用 `/pendo web token` 私聊获得的一次性登录链接登录。链接 5 分钟内仅可兑换一次；浏览器随后使用短期 HttpOnly session cookie，不会把 bearer 写入 localStorage。

默认 loopback HTTP 为本机开发保留非 Secure cookie 兼容性。若把 `PENDO_WEB_HOST` 改为非 loopback 地址，必须放在 HTTPS 反向代理之后，并在启动前显式设置：

```text
# PowerShell
$env:PENDO_WEB_SESSION_COOKIE_SECURE="true"
python main.py

# bash
PENDO_WEB_SESSION_COOKIE_SECURE=true python main.py
```

Web 控制台包含总览、日程、待办、账本、笔记、日记、搜索、统计、设置和迁移页面。迁移页面负责 `.pendo.zip` Bundle 的预览、导出、导入、冲突策略和审计日志；聊天端 `/pendo export` 只导出 Markdown 档案。

Pendo Web 的公开 demo 会话默认关闭。如需临时开启演示环境，可在启动主进程前设置：

```text
# PowerShell
$env:PENDO_WEB_DEMO_ENABLED="1"
python main.py

# bash
PENDO_WEB_DEMO_ENABLED=1 python main.py
```

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
- `session_timeout`：影响活跃会话的 Step F 处理
- `plugins.smalltalk_provider`：影响 Step G 的 smalltalk 回落

**分发顺序**（固定，不可配置）：
1. URL-only 短路
2. 处理门控
3. 只喊名字回应
4. 命令匹配
5. 未知命令提示
6. 活跃会话
7. smalltalk 回落
