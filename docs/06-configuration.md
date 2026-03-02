# 06 - 配置详解

本章详细说明所有配置项。

---

## 配置文件

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
      "base_url": "https://api.openai.com/v1"
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
api_key = context.secrets.get("myplugin", {}).get("api_key")
```

#### xiaoqing_chat 配置

```json
{
  "plugins": {
    "xiaoqing_chat": {
      "api_key": "sk-your-openai-api-key",
      "base_url": "https://api.openai.com/v1",
      "model": "gpt-4o-mini",
      "temperature": 0.7,
      "max_tokens": 1000,
      "memory_enabled": true,
      "emotion_enabled": true,
      "expression_learning": true
    }
  }
}
```

**配置项说明**：

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|---------|------|
| `api_key` | `string` | 必需 | LLM API Key |
| `base_url` | `string` | `https://api.openai.com/v1` | API 基础 URL |
| `model` | `string` | `gpt-4o-mini` | 使用的模型 |
| `temperature` | `float` | `0.7` | 温度参数（0-1） |
| `max_tokens` | `int` | `1000` | 最大 token 数 |
| `memory_enabled` | `boolean` | `true` | 是否启用长期记忆 |
| `emotion_enabled` | `boolean` | `true` | 是否启用情绪系统 |
| `expression_learning` | `boolean` | `true` | 是否启用表情学习 |

**支持的 API 提供商**：

```json
// OpenAI
{
  "base_url": "https://api.openai.com/v1",
  "model": "gpt-4o-mini"
}

// Azure OpenAI
{
  "base_url": "https://your-resource.openai.azure.com/openai/deployments/your-deployment",
  "api_key": "your-azure-key"
}

// 兼容 OpenAI 的服务（如 DeepSeek、Qwen）
{
  "base_url": "https://api.deepseek.com/v1",
  "model": "deepseek-chat"
}
```

#### pendo 配置

```json
{
  "plugins": {
    "pendo": {
      "reminder_enabled": true,
      "daily_briefing_time": "08:00",
      "evening_briefing_time": "21:00",
      "diary_reminder_time": "23:00",
      "timezone": "Asia/Shanghai"
    }
  }
}
```

**配置项说明**：

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|---------|------|
| `reminder_enabled` | `boolean` | `true` | 是否启用提醒 |
| `daily_briefing_time` | `string` | `"08:00"` | 每日简报时间 |
| `evening_briefing_time` | `string` | `"21:00"` | 晚间简报时间 |
| `diary_reminder_time` | `string` | `"23:00"` | 日记提醒时间 |
| `timezone` | `string` | `"Asia/Shanghai"` | 时区 |

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
      "api_key": "your-production-api-key",
      "model": "gpt-4o-mini",
      "temperature": 0.7
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
      "api_key": "sk-your-openai-api-key",
      "base_url": "https://api.openai.com/v1",
      "model": "gpt-4o-mini",
      "temperature": 0.7,
      "memory_enabled": true,
      "emotion_enabled": true,
      "expression_learning": true
    },
    "pendo": {
      "reminder_enabled": true,
      "daily_briefing_time": "08:00",
      "evening_briefing_time": "21:00"
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
      "api_key": "your-api-key",
      "base_url": "https://api.deepseek.com/v1",
      "model": "deepseek-chat",
      "temperature": 0.5
    }
  }
}
```

**推荐免费/低成本选项**：
- **DeepSeek**：`https://api.deepseek.com/v1` - 免费额度较大
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

配置文件支持热重载：

```
/reload config
```

或在代码中：
```python
context.reload_config()
```

**注意**：某些配置（如 `inbound_http_base` / `inbound_ws_uri`）需要重启才能生效。

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

## 下一步

- 高级主题 → [07-advanced.md](07-advanced.md)

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
