# 🛠️ 06 - 配置详解

本章说明 XiaoQing Core 的公开配置、敏感配置、AI 注册表、插件命名空间、网络边界和热重载语义。插件专项字段由各插件 README 维护。

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

Core 将两个文件组合成带 revision 的只读设置快照。插件通过作用域 Context 读取自身命名空间。

`config.json` 顶层 `ai` 对象维护统一模型注册表，顶层 `plugins` 对象维护插件公开命名空间；`secrets.json` 使用同名 `ai` 与 `plugins` 对象保存对应凭据。

---

## ⚙️ Core 配置

### 机器人与命令

| 字段 | 类型 | 示例值 | 说明 |
|---|---|---|---|
| `bot_name` | string | `"小青"` | Bot 名称、名称召唤和群聊名称门控 |
| `command_prefixes` | string[] | `["/"]` | 命令前缀列表 |
| `require_bot_name_in_group` | boolean | `true` | 群聊普通文本的 Bot 名称门控 |
| `default_group_ids` | positive integer[] | `[123456789]` | `broadcast` / `targeted` schedule 未指定 `group_ids` 时的群目标 |
| `timezone` | IANA timezone | `"Asia/Shanghai"` | Scheduler、`context.now()` 和业务日期时区 |

`command_prefixes` 可配置多个前缀。Manifest `triggers` 保存纯命令词，Router 在消息解析阶段处理前缀。这五项通过 `/reload` 发布到新 revision，并触发插件后台重载。

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

启用 HTTP 或 WebSocket Inbound 时，`secrets.json` 顶层 `inbound_token` 使用非空字符串。HTTP 与 WebSocket 共享该 token、接纳队列和会话排序。

主动 WebSocket 与 HTTP Action 通过 `secrets.json` 顶层 `onebot_token` 使用 Bearer 鉴权。有效 secrets 快照中的空字符串表示双方约定的匿名 OneBot 连接。

Inbound 的接纳容量为 `inbound_ws_max_workers + ws_queue_size`。同一私聊用户、同一群与用户组合各自按接纳顺序串行执行，各会话键之间共享 worker 并行处理。达到容量时 HTTP 返回过载状态，WebSocket 返回过载响应。

### Inbound 网络边界

默认 Listener 地址使用 loopback。公网入口采用以下路径：

```text
公网 HTTPS/WSS
  → Nginx / Caddy TLS 终止与访问控制
  → loopback HTTP/WS
  → XiaoQing Inbound
```

跨容器受控网络可设置 `inbound_trusted_tls_proxy: true`，并通过防火墙将 Listener 流量限定到代理来源。

`inbound_http_base` 使用 `http://`，`inbound_ws_uri` 使用 `ws://`。XiaoQing Listener 负责明文 HTTP/WS 协议，TLS 终止由外层代理或网络层提供。配置非 loopback 主机时同时设置 `inbound_trusted_tls_proxy: true`，并把主机防火墙入口限定到代理来源。

### 运行时与数据

| 字段 | 类型 | 默认值与范围 | 生效方式 | 说明 |
|---|---|---|---|---|
| `max_concurrency` | integer | `5`，`1..1024` | 热重载 | Dispatcher 全局活动消息上限 |
| `data_root` | path string | `"data"` | 重启 | 插件运行数据根目录 |
| `enable_plugin_watcher` | boolean | `false` | 热重载 | 插件文件 watcher 开关 |
| `plugin_poll_interval` | number | `3600`，`0.01..86400` | 热重载 | 插件文件轮询秒数 |
| `session_timeout` | number | `300`，`0 < value <= 604800` | 热重载 | Session 默认空闲秒数 |
| `timezone` | IANA timezone | `"Asia/Shanghai"` | 热重载 | Scheduler 与业务时钟时区 |

`data_root` 可使用项目相对路径或部署主机绝对路径，并保持在 `plugins/` 源码树之外。每个插件获得 `data_root/<plugin_name>/`。Core 首次发现 `plugins/<name>/data/` 时，将内容原子迁移到新数据根，并把来源目录归档到 `data_root/.legacy-plugin-data/<name>/`。修改 `data_root` 后重启主进程。

插件 watcher 在启动时运行解释器能力探针。通过探针的环境支持热重载，restart-only 环境通过重启进程应用源码变化。Watcher 监控插件 Python、根目录 JSON 与 Manifest `watch_files`，只发布通过完整稳定快照校验的新插件代。

### Windows 生产启动

| 字段 | 示例 | 约束 | 说明 |
|---|---|---|---|
| `napcat_account` | `"1234567890"` | 空字符串或 5～20 位十进制数字 | NapCat 启动位置参数 |
| `mkl_threading_layer` | `"TBB"` | 空字符串或最长 64 字符的字母、数字、点、下划线和连字符 | Bot 子进程的 `MKL_THREADING_LAYER` |

`scripts/run-bot-monitor.ps1` 在创建子进程前读取这些字段。启动链调用当前 `PATH` 中的 Python，并保持部署环境对解释器和依赖的所有权。字段修改在下次启动监控链时生效。

`scripts/stop-bot.vbs` 是 Windows 双击停服入口。它调用监控器的 `-Stop` 模式，通过仓库级互斥量阻止停服期间产生新实例，并按当前仓库脚本路径与 NapCat 可执行文件路径回收进程树。CIM 命令行读取采用三次有界重试；提升权限的 SSH、计划任务或管理员终端创建进程时，普通桌面入口显示一次 UAC，提升后的停止实例重新校验 PID、进程名和绝对命令路径。停服完成后可双击 `scripts/run-bot.vbs` 重新启动。

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
      "codex": {"timeout_seconds": 0},
      "shell": {"timeout_seconds": 0}
    }
  }
}
```

| 字段 | 范围 | 说明 |
|---|---|---|
| `timeout_seconds` | `0` 或 `0.1..86400` | 单次插件入口预算；`0` 由插件自身生命周期控制 |
| `parallel_limit` | `1..1024` | `parallel` 插件入口并发 |
| `admission_queue_limit` | `0..10000` | 插件入口等待队列 |
| `sync_parallel_limit` | `1..3` | 单插件同步 worker 并发 |
| `sync_queue_limit` | `0..10000` | 单插件同步等待队列 |
| `failure_threshold` | `1..10000` | 熔断计数阈值 |
| `cooldown_seconds` | `0.1..86400` | 熔断冷却时间 |
| `drain_timeout_seconds` | `0.1..3600` | 卸载排空预算 |
| `global_sync_queue_limit` | `1..100000` | 全局同步等待队列 |
| `overrides` | object | 按插件覆盖上述策略 |

Manifest `concurrency: sequential` 将该插件入口并发固定为 1；`parallel` 使用 `parallel_limit`。同步库通过 `run_sync()` 进入同步预算。

`admission_queue_limit` 与 `sync_queue_limit` 表示等待槽数量，值 `0` 对应零等待槽。插件入口总接纳容量等于活动槽与等待槽之和。`overrides.<plugin_name>` 可提供任意策略子集，省略字段继承全局策略；JSON `null` 会触发配置校验错误。失败次数达到 `failure_threshold` 后执行 gate 进入 `cooldown_seconds` 冷却，插件重载使用 `drain_timeout_seconds` 等待当前代收尾。

---

## ⚙️ 日志配置

| 字段 | 类型 | 示例值 | 说明 |
|---|---|---|---|
| `log_level` | string | `"INFO"` | 根日志级别，常用值为 `DEBUG`、`INFO`、`WARNING`、`ERROR` |
| `log_to_file` | boolean | `true` | 写入 `logs/xiaoqing.log` 与 `logs/xiaoqing_error.log` |
| `log_to_console` | boolean | `true` | 控制台输出 |
| `log_use_color` | boolean | `true` | 交互终端 ANSI 颜色 |
| `log_max_size_mb` | number | `10` | `size` 模式主日志与错误日志的单文件阈值 |
| `log_backup_count` | integer | `5` | 轮转文件保留数 |
| `log_rotation` | `time` / `size` | `"time"` | 主日志每日午夜轮转，或按大小轮转 |

错误日志固定记录 `ERROR` 及以上并按大小轮转。插件日志器自动附加插件名和 request ID。Core 与插件记录 token、Cookie、授权头和密钥字段时使用摘要或脱敏值。日志 handler 在进程启动阶段创建，修改本节字段后重启主进程。

---

## 🧠 AI/VLM 注册表

统一 AI 配置分为四层：

1. `config.ai.providers`：服务地址与公开连接参数。
2. `config.ai.models`：可复用模型 profile、模型名、模态和请求默认值。
3. `config.plugins.<name>.ai.routes`：插件任务到模型 profile 的有序链。
4. `secrets.ai.providers`：Provider API Key。

### Provider

```json
{
  "ai": {
    "providers": {
      "deepseek": {
        "api_base": "https://api.deepseek.com",
        "endpoint_path": "/chat/completions",
        "proxy": ""
      }
    }
  }
}
```

| 字段 | 类型 | 默认值 | 规则 |
|---|---|---|---|
| `api_base` | absolute HTTP(S) URL | 必填 | 包含 scheme 与主机，省略凭据、query 和 fragment |
| `endpoint_path` | absolute URL path | `/chat/completions` | 以 `/` 开头，省略 query 与 fragment |
| `proxy` | absolute HTTP(S) URL or empty string | `""` | 当前 Provider 的可选代理 |

### 模型 profile

```json
{
  "ai": {
    "models": {
      "deepseek-flash": {
        "provider": "deepseek",
        "model": "deepseek-v4-flash",
        "modalities": ["text"],
        "request_defaults": {
          "thinking": {"type": "disabled"}
        }
      }
    }
  }
}
```

| 字段 | 类型 | 规则 |
|---|---|---|
| `provider` | string | 引用 `config.ai.providers` 中的名称 |
| `model` | string | 发送给上游 API 的模型名 |
| `modalities` | string[] | 非空集合，成员为 `text`、`image`、`audio` |
| `request_defaults` | object | 模型级附加请求字段；Core 保留 `model`、`messages`、`stream`、采样参数和工具字段 |

### 插件 route

```json
{
  "plugins": {
    "xiaoqing_chat": {
      "ai": {
        "routes": {
          "chat": {
            "models": ["deepseek-flash", "glm-5.2"],
            "temperature": 0.8,
            "max_tokens": 2000,
            "timeout_seconds": 30,
            "total_timeout_seconds": 60
          }
        }
      }
    }
  }
}
```

| 字段 | 默认值与范围 | 说明 |
|---|---|---|
| `models` | 必填非空列表 | 按顺序排列主 profile 与 fallback profile |
| `temperature` | 可选，`0..2` | route 采样温度 |
| `top_p` | 可选，`0..1` | route nucleus sampling |
| `max_tokens` | 可选，`1..1000000` | route 输出 token 上限 |
| `timeout_seconds` | `30`，`0.1..600` | 单次上游请求预算 |
| `total_timeout_seconds` | `max(timeout, 60)`，`0.1..1800` | 全部重试与 fallback 的总预算 |
| `max_retry` | `1`，`0..10` | 每个 profile 的重试次数 |
| `retry_interval_seconds` | `0.5`，`0..60` | 指数退避基数 |
| `fallback_on` | Core 默认错误集合 | 触发下一 profile 的错误类别 |
| `request_defaults` | `{}` | route 级附加请求字段 |

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

每个 Provider 在 `secrets.ai.providers.<provider_name>.api_key` 保存 API Key。模型级 `request_defaults` 先合入请求，route 级默认值随后覆盖，调用参数最后覆盖采样与预算字段。

AI Service 先调用 route 首个 profile，并对 transport、timeout、rate limit、server error、model unavailable、invalid response 和 empty response 执行有界重试与顺序 fallback。`fallback_on` 还可选择 `request_timeout` 与 `conflict`。认证、权限、请求参数和配置错误由当前调用插件处理。`total_timeout_seconds` 覆盖整条模型链。

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
| `plugins.pendo.ai.routes.parse` | route object | 自然语言结构化解析模型链 |

Pendo 的五个 Web 字段按配置 revision 原子发布，监听地址变化会重建插件拥有的 Web Server。登录 Code 与浏览器会话有效期为 7 天，Widget Token 有效期为 365 天。[Pendo 配置与 Web](../plugins/pendo/README.md#web-控制台) 说明认证、Demo 与数据目录。

### ADS Paper 与 XiaoQing Chat AI

| 路径 | 说明 |
|---|---|
| `plugins.ads_paper.ai.routes.summary` | ADS 论文 AI 摘要 route |
| `plugins.xiaoqing_chat.ai.routes.chat` | 主回复 route |
| `plugins.xiaoqing_chat.ai.routes.checker` | 回复检查 route |
| `plugins.xiaoqing_chat.ai.routes.reasoning` | 规划与推理 route |
| `plugins.xiaoqing_chat.ai.routes.vision` | 图片理解 route，profile 需要 `image` modality |
| `plugins.xiaoqing_chat.ai.default_model_alias` | `/xc model default` 使用的别名 |
| `plugins.xiaoqing_chat.ai.model_aliases` | 用户别名到 route profile 的映射 |

四条 XiaoQing Chat route 分别拥有模型顺序、单次超时、总超时和重试策略。行为、参与、记忆和表达参数位于 `plugins/xiaoqing_chat/config/xiaoqing_config.json`；[XiaoQing Chat 配置](../plugins/xiaoqing_chat/README.md#行为配置) 逐组说明这些字段。

### QingSSH

公开预算字段位于 `plugins.qingssh`：

| 字段组 | 示例字段 | 作用 |
|---|---|---|
| 命令 | `command_timeout_seconds=30` | 远端命令预算；`0` 交给管理员主动停止 |
| QQ action | `qq_max_actions=6` | 单次结果 action 数 |
| QQ 文本 | `qq_max_text_chars=10000`、`qq_max_message_chars=1800` | 累计文本与单消息预算 |
| 首尾预览 | `qq_head_chars=6000`、`qq_tail_chars=2000` | 长输出预览保留量 |
| 投递 | `qq_send_interval_seconds=0.35`、`qq_send_timeout_seconds=5` | action 间隔与单次等待预算 |
| 归档 | `archive_max_bytes=67108864`、`archive_tail_bytes=1048576`、`archive_retention_files=20` | 命令归档大小、尾部与数量预算 |

连接定义、Host Key、认证材料和跳板设置位于 `secrets.plugins.qingssh`，由 `/ssh add` 等管理员命令管理。[QingSSH 配置](../plugins/qingssh/README.md#服务器配置) 提供连接 schema。

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

工作目录、允许根目录、保留期、紧急磁盘阈值和 arXiv 摘要会话等扩展字段见 [Codex 配置](../plugins/codex/README.md#基础配置)。

### Shell 与 Smalltalk

| 路径 | 示例 | 说明 |
|---|---|---|
| `plugins.shell.terminal.backend` | `"direct"` | `direct` 通过 `create_subprocess_exec` 启动外部程序；`git-bash` 通过 Git Bash 执行命令文本 |
| `plugins.shell.terminal.executable` | Git Bash 绝对路径 | `git-bash` 后端的解释器路径 |
| `plugins.smalltalk.voice_probability` | `0` | 纯文本闲聊转语音概率，范围 `0..1` |

Shell 命令启用列表、工作目录与审计开关位于 `secrets.plugins.shell`；[Shell 配置](../plugins/shell/README.md#终端配置) 说明两种后端。[插件使用手册](09-plugins.md) 说明全部 30 个插件的配置入口与数据位置。

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

`inbound_token` 在启用 Inbound Listener 时使用非空高熵字符串。`onebot_token` 与 OneBot 实现保持一致；空字符串对应双方约定的匿名模式。`admin_user_ids` 使用正整数 QQ 列表。

示例 secrets 中的插件字段：

| 命名空间 | 字段 | 用途 |
|---|---|---|
| `plugins.arxiv_filter` | `feishu_webhook` | 飞书推送 Webhook |
| `plugins.flickr` | `api_key` | Flickr App Garden 公共只读 API Key |
| `plugins.wolframalpha` | `appid` | Wolfram\|Alpha App ID |
| `plugins.voice` | `subscription_key`、`region`、`voice_name`、`style`、`role`、`proxy` | Azure Speech 凭据、音色与代理 |
| `plugins.chat` | `token`、`bot_id`、`proxy` | Coze API 凭据与代理 |
| `plugins.twitter` | `user_id`、`headers.authorization`、`cookies`、`proxy`、`max_pages` | X/Twitter 抓取身份、会话和页数预算 |
| `plugins.signin.yingshijufeng` | `app_id`、`kdt_id`、`access_token`、`sid` | 影视飓风有赞签到凭据 |
| `plugins.ads_paper` | `ads_token` | NASA ADS API Token |

QingSSH、Shell 与 Codex 的管理员命令会维护各自 secret 命名空间；字段 schema 位于对应插件 README。生产 secrets 使用随机高熵 token，并通过文件权限、备份策略和主机访问控制保护。反向代理可增加来源限制、速率限制与外层认证。

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
| 有效的外部 `secrets.json` 单文件候选 | 管理员核对私聊字段摘要后执行 `/reload` |
| `data_root`、日志字段、`napcat_account`、`mkl_threading_layer` | 重启主进程或生产启动链 |
| 插件源码、Manifest 与 `watch_files` | `/reload`、插件 watcher 或 restart-only 环境的进程重启 |

普通配置校验异常时继续使用 last-known-good 快照。敏感来源缺失、损坏、不可读或与新公开配置尚未确认配对时立即进入 fail-closed 状态，撤销网络凭据与运行时管理员视图。管理员私聊中的 `/set_secret` 适用于已有 secret 路径，由 Core 完成持锁写入和 revision 发布。

配置 watcher 将分别保存的新文件代视为独立来源事件。当前公开配置签名与已确认版本一致、当前来源对可信且外部 secrets 文件完整有效时，新文件代作为待确认候选保存；运行时继续使用已确认 secrets，revision 与凭据代保持当前值。Core 使用当前可信 OneBot 通道私聊全部当前管理员，通知包含文件有效状态及新增、删除、修改的字段路径，所有字段值保持隐藏。`/reload` 会重新读取稳定磁盘来源并原子发布候选。

新公开配置可先发布；与该来源尚未确认配对的 secrets 进入 `INCONSISTENT` 撤权状态。主动 WebSocket 是唯一控制通道的部署采用“停止服务 → 原子保存两个完整文件 → 启动服务”，启动读取会确认新的来源对。保留独立受保护 Inbound 的实例可在文件稳定后通过该通道执行 `/reload`。

单个配置文件上限为 8 MiB，JSON 树深度上限为 64，节点上限为 100000。Core 拒绝非有限数字、重复不稳定读取和超出预算的来源。部署脚本在停服窗口内先写临时文件并原子替换，再启动服务。

网络地址或 token 变化会唤醒主动 WebSocket 任务，并通过候选 Listener 提交 Inbound 新代。同一时间只有已提交代接纳新事件。

---

## ✅ 生产配置检查表

- `bot_name`、命令前缀和默认群符合目标 QQ 场景。
- `inbound_token` 使用高熵随机值。
- Inbound Listener 绑定 loopback 或受控代理网络。
- 反向代理负责 TLS、来源策略和速率限制。
- `admin_user_ids` 只包含当前管理员。
- AI Key 与插件凭据位于 secrets。
- 有效的 secrets 单文件替换由管理员核对字段摘要并执行 `/reload`；公开配置变更与双来源替换安排在停服窗口内。
- `data_root`、日志目录和备份目录具有明确所有权。
- `plugin_execution` 为长任务插件配置合适预算。
- Pendo Web Cookie Secure 与 HTTPS 部署保持一致。
- 正式启动前完成 `bash scripts/run_full_uat.sh --plan-only` 和完整 UAT。

---

## 🧭 下一步

- 首次部署：[快速开始](01-getting-started.md)
- 插件字段：[插件目录](09-plugins.md)
- 网络与消息路径：[消息处理流程](08-message-flow.md)
