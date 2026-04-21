# 🔧 06 - 配置详解

本章详细说明所有配置项。

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
  "random_reply_rate": 0.05,
  "default_group_ids": [],
  
  "enable_ws_client": false,
  "enable_inbound_server": true,
  
  "onebot_ws_uri": "ws://127.0.0.1:11000/ws",
  "onebot_http_base": "http://127.0.0.1:11001",
  
  "inbound_ws_uri": "ws://127.0.0.1:12000/ws",
  "inbound_http_base": "http://127.0.0.1:12000",
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
    "smalltalk_provider": "smalltalk"
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
- **说明**：群聊随机回复概率。即使不满足触发条件，也有此概率回复。

```json
{"random_reply_rate": 0.1}  // 10% 概率
```

#### default_group_ids
- **类型**：`int[]`
- **默认**：`[]`
- **说明**：默认发送群列表，用于定时任务等。

```json
{"default_group_ids": [123456, 789012]}
```

### 通信配置

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
- **说明**：Inbound HTTP 服务监听地址（提供 `/event`、`/health`、`/metrics`）。留空则不启动 HTTP Inbound。

#### inbound_ws_uri
- **类型**：`string`
- **默认**：`"ws://127.0.0.1:12000/ws"`
- **说明**：Inbound WebSocket 服务监听地址（仅 WS）。支持与 `inbound_http_base` 使用不同端口；留空则不启动 WS Inbound。

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

**推荐配置**：
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
- 启用媒体配置后可把图片消息写入上下文，并在文本回复后追加本地表情包
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
- 当使用 `xiaoqing_chat` 时，所有群聊消息都会进入 `handle_smalltalk()`
- `random_reply_rate` 配置失效
- 由插件内部控制回复频率（更智能）
- 支持向量数据库长期记忆

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
> `xiaoqing_chat` 现在按 provider 结构读取 secrets。这样可以配多套聊天模型，并通过 `/xc 模型 <名称>` 在运行时切换。

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
    "enable_outbound_image_reply": true,
    "enable_outbound_emoji_reply": true,
    "enable_outbound_face_reply": true,
    "image_library_dir": "figures/reply_images",
    "emoji_library_dir": "figures/library",
    "image_reply_probability": 0.12,
    "emoji_reply_probability": 0.35,
    "face_reply_probability": 0.18,
    "max_media_per_message": 3,
    "vision_provider": "vision"
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
| `enable_outbound_image_reply` | `boolean` | `true` | 是否允许回复阶段补发本地图片 |
| `enable_outbound_emoji_reply` | `boolean` | `true` | 是否允许回复阶段发送本地表情包 |
| `enable_outbound_face_reply` | `boolean` | `true` | 是否允许回复阶段补发 QQ 原生 `face` 表情 |
| `image_library_dir` | `string` | `"figures/reply_images"` | 本地可发送图片目录。相对路径按 `plugins/xiaoqing_chat/` 解析 |
| `emoji_library_dir` | `string` | `"figures/library"` | 本地可发送表情包目录。相对路径按 `plugins/xiaoqing_chat/` 解析 |
| `image_reply_probability` | `float` | `0.12` | 满足条件时启用图片模态选择的概率 |
| `emoji_reply_probability` | `float` | `0.35` | 满足条件时启用表情包模态选择的概率 |
| `face_reply_probability` | `float` | `0.18` | 满足条件时启用 QQ 表情模态选择的概率 |
| `max_media_per_message` | `int` | `3` | 单条回复最多附带多少个媒体模态；只发媒体时仍会优先只保留一个“only”模态 |
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
            "endpoint_path": "/chat/completions"
          }
        }
      }
    }
  }
}
```

**缓存与索引位置**：

- 收到的图片：`plugins/xiaoqing_chat/figures/inbox/`
- 可发送表情包：`plugins/xiaoqing_chat/figures/library/`
- 图片描述缓存：`plugins/xiaoqing_chat/data/media/render_cache.json`
- 表情包索引：`plugins/xiaoqing_chat/figures/library/index.json`

插件会把入站图片统一落到 `figures/inbox/`。如果图片被识别成表情包，就会自动复制进 `figures/library/` 并写入索引，后续在合适语境下参与表情包回复选择。新收进图库的表情包也会让这条消息更倾向于触发一次自然回应。

出站阶段不会因为发现旧图库条目缺少 `description` / `marker` / `emotion_tags` 就同步重跑视觉模型。当前回复会先基于已有元数据和文本上下文做决策，坏条目的补修会放到后台异步执行，不阻塞这次回复。

NapCat/OneBot 的纯 `mface` 消息如果没有直接携带图片源，插件会尝试通过 `onebot_http_base` 对应的 HTTP API 调用 `get_msg` 和 `get_image` 回收真实图片；拿不到真实图片时，再退回成仅保留摘要的 `[表情包：...]` 标记。

QQ 原生 `face` 表情不会走图片下载链，而是直接转换成 `[QQ表情：微笑]` 这类 marker 进入上下文；如果拿不到名称，则退回成 `[QQ表情：id=14]`。

如果视觉模型未配置、配置不完整，或请求失败，插件会退回到基于文件名/摘要的保守标记，不会阻断普通文本对话；同时会在日志里打出 `media.analyze.skip` / `media.analyze.fail`，方便定位是“没拿到图”还是“视觉模型没跑起来”。

如果你后来补上了显式视觉配置（例如 `vision.default`，或者 `media.vision_provider` 指向一个单独的视觉 provider），旧的低质量 fallback 图片缓存会在再次命中时自动重跑识图并覆盖，不需要手工清空整个 `render_cache.json`。

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

# Token 签名密钥优先级：
# 1. 环境变量 PENDO_WEB_TOKEN_SECRET
# 2. plugins/pendo/data/web_token_secret.txt（首次运行自动生成）
```

**Web 服务监听地址**：

```text
# PowerShell
$env:PENDO_WEB_HOST="127.0.0.1"
$env:PENDO_WEB_PORT="8765"

# bash
PENDO_WEB_HOST=127.0.0.1
PENDO_WEB_PORT=8765
```

如果你在 Windows 上遇到 `WinError 10013`，通常不是已有进程占用了端口，而是系统拒绝绑定。此时优先换一个端口，例如 `PENDO_WEB_PORT=8766`。

用户偏好（时区、简报时间、日记提醒等）通过 `/pendo settings` 命令在运行时修改，存储于数据库，无需修改配置文件。

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
- QingSSH 现在严格校验 `~/.ssh/known_hosts` 中的 Host Key；未知主机或 Host Key 变更不会自动放行。
- 从 `~/.ssh/config` 导入时，支持 `ProxyJump` 以及安全的 `ssh -W` 跳板形式；其他会在本地执行命令的 `ProxyCommand` 会被拒绝。

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

## 配置最佳实践

### 1. 开发环境
```json
// config.json
{
  "log_level": "DEBUG",
  "random_reply_rate": 0,
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
  "random_reply_rate": 0.05,
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
            "endpoint_path": "/chat/completions"
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
  "random_reply_rate": 0.05,  // 使用 xiaoqing_chat 时此配置失效
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
            "endpoint_path": "/chat/completions"
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
```json
// config.json
{
  "inbound_http_base": "http://0.0.0.0:12000",
  "inbound_ws_uri": "",
  "log_level": "INFO"
}

// secrets.json
{
  "inbound_token": "strong-random-token-here",
  "onebot_token": "your-onebot-token",
  "admin_user_ids": [123456789]
}
```

⚠️ **公网部署必须设置 `inbound_token`！**

⚠️ **公网部署建议使用强密码作为 Token！**

---

## 环境变量

目前 XiaoQing 不支持环境变量配置。如需此功能，可以修改 `config.py`：

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
- 常用运行时配置都支持热重载；`enable_ws_client`、`onebot_ws_uri`、`enable_inbound_server`、`inbound_http_base`、`inbound_ws_uri`、`ws_queue_size`、`inbound_ws_max_workers`、`max_concurrency`、`session_timeout`、`timezone` 等修改后，可通过 watcher 或 `/reload config` 直接生效。
- 配置 watcher 默认开启；插件 watcher 只有在 `enable_plugin_watcher=true` 时才会自动 reload 插件，且会忽略 `plugins/*/data/` 下的运行时状态文件。
- 如果 `config.json` 或 `secrets.json` 一次写坏，运行时会保留最后一次有效快照；修复文件后重新 `/reload config` 即可。
- 自动 watcher 能发现文件变化，但如果你刚修改完配置、希望立即生效，仍然建议手动执行一次 `/reload`。

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
/pendo web start           # 启动，默认 127.0.0.1:8765

# 如需改端口，在启动主进程前设置环境变量
# PowerShell
$env:PENDO_WEB_PORT="8766"
python main.py

# bash
PENDO_WEB_PORT=8766 python main.py
```

访问 `http://127.0.0.1:8765`（或你自定义的新端口），使用 `/pendo web token` 获取的 Token 登录。

Pendo Web 的公开 demo 会话默认关闭。如需临时开启演示环境，可在启动主进程前设置：

```text
# PowerShell
$env:PENDO_WEB_DEMO_ENABLED="1"
python main.py

# bash
PENDO_WEB_DEMO_ENABLED=1 python main.py
```

### nginx 子路径反向代理

如果希望将 pendo Web 部署在子路径 `/pendo/`（而非根路径），可以通过 nginx 做前缀转发。当前前端静态资源和 API 请求都使用相对路径，因此一个带尾部 `/` 的 `proxy_pass` 即可同时覆盖页面与 `/api/*`：

```nginx
# nginx.conf 片段
location = /pendo {
    return 301 /pendo/;
}

location /pendo/ {
    proxy_pass http://127.0.0.1:8765/;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

**注意**：
- 上面这条 `proxy_pass http://127.0.0.1:8765/;` 会把 `/pendo/...` 转成后端根路径 `/...`，因此浏览器里的 `/pendo/api/...` 会自动映射到后端的 `/api/...`
- pendo 后端真实 API 前缀是 `/api/`，不是 `/pendo/api/`
- 环境变量 `PENDO_WEB_TOKEN_SECRET` 可用于在多实例/重启场景下保持 Token 签名密钥稳定

---

## Handler 链相关配置

框架引入了 Handler 链式处理，无需额外配置即可启用。

**相关配置**：
- `bot_name`：影响 BotNameHandler 的触发
- `command_prefixes`：影响 CommandHandler 的匹配
- `session_timeout`：影响 SessionHandler 的超时
- `plugins.smalltalk_provider`：影响 SmalltalkHandler 的调用

**Handler 优先级**（固定，不可配置）：
1. BotNameHandler
2. CommandHandler
3. SessionHandler
4. SmalltalkHandler
