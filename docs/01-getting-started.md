# 01 - 快速开始

本章将帮助你在 10 分钟内完成 XiaoQing 的安装、配置和运行。

---

## 环境要求

- **Python 3.10+**（推荐 3.11）
- **OneBot 实现**：用于连接 QQ
  - 推荐 [NapCatQQ](https://github.com/NapNeko/NapCatQQ) (Modern OneBot 11 Implementation)

---

## 第一步：安装依赖

```bash
cd XiaoQing
pip install -r requirements.txt
```

核心依赖包括：
- `aiohttp` - 异步 HTTP
- `websockets` - WebSocket 通信
- `apscheduler` - 定时任务

---

## 第二步：配置文件

XiaoQing 使用两个配置文件：

### config/config.json - 基础配置

```json
{
  "bot_name": "小青",
  "command_prefixes": ["/"],
  "require_bot_name_in_group": true,
  "random_reply_rate": 0.05,
  
  "enable_ws_client": false,
  "enable_inbound_server": true,
  
  "onebot_http_base": "http://127.0.0.1:11001",
  "inbound_http_base": "http://127.0.0.1:12000",
  "inbound_ws_uri": "ws://127.0.0.1:12000/ws",
  
  "max_concurrency": 5,
  "session_timeout": 300,
  "timezone": "Asia/Shanghai",
  "log_level": "INFO",
  
  "plugins": {
    "smalltalk_provider": "xiaoqing_chat"
  }
}
```

**关键配置说明**：

| 配置项 | 说明 | 建议值 |
|--------|------|--------|
| `bot_name` | 机器人名称，群聊中喊这个名字会触发 | 你喜欢的名字 |
| `command_prefixes` | 命令前缀，如 `/help` | `["/"]` |
| `onebot_http_base` | OneBot 的 HTTP API 地址 | 根据你的 OneBot 配置 |
| `inbound_http_base` | XiaoQing Inbound HTTP（接收 OneBot 推送） | `http://127.0.0.1:12000` |
| `plugins.smalltalk_provider` | 闲聊提供者插件 | `xiaoqing_chat` 或 `smalltalk` |

### config/secrets.json - 敏感配置

```json
{
  "onebot_token": "",
  "inbound_token": "your-secret-token",
  "admin_user_ids": [123456789],
  "plugins": {}
}
```

**关键配置说明**：

| 配置项 | 说明 |
|--------|------|
| `inbound_token` | 与 OneBot 通信的密钥，需要双方一致 |
| `admin_user_ids` | 管理员 QQ 号列表，可执行管理命令 |
| `plugins` | 各插件的私有配置（如 API Key） |

⚠️ **注意**：`secrets.json` 包含敏感信息，不要提交到 Git！

---

## 第三步：配置 OneBot

XiaoQing 支持两种通信模式：

### 模式一：被动接收（推荐）

XiaoQing 启动一个 HTTP 服务器，OneBot 主动把消息推送过来。

```
OneBot (NapCat) ──POST──> XiaoQing (端口 12000)
```

**XiaoQing 配置**：
```json
{
  "enable_ws_client": false,
  "enable_inbound_server": true,
  "inbound_http_base": "http://127.0.0.1:12000",
  "inbound_ws_uri": ""
}
```

**NapCat 配置**（onebot11.json 或 WebUI）：
```json
{
  "http": {
    "enable": true,
    "host": "127.0.0.1",
    "port": 11001,
    "secret": "",
    "enableHeart": false,
    "enablePost": true,
    "postUrls": [
      "http://127.0.0.1:12000/event"
    ]
  }
}
```

### 模式二：主动连接

XiaoQing 主动连接 OneBot 的 WebSocket。

```
XiaoQing ──WebSocket──> OneBot (端口 11000)
```

**XiaoQing 配置**：
```json
{
  "enable_ws_client": true,
  "enable_inbound_server": false,
  "onebot_ws_uri": "ws://127.0.0.1:11000/ws"
}
```

**NapCat 配置**：
```json
{
  "ws": {
    "enable": true,
    "host": "127.0.0.1",
    "port": 11000
  }
}
```

### 选择哪种模式？

| 模式 | 优点 | 缺点 |
|------|------|------|
| 被动接收 | 简单可靠，支持响应返回 | 需要 XiaoQing 监听端口 |
| 主动连接 | 不需要 XiaoQing 监听 | 需要 OneBot 启用 WS |

**推荐使用被动接收模式**，配置简单，调试方便。

---

## 第四步：启动

### 启动 OneBot

先启动你的 NapCat：

```bash
# Windows
./NapCat.exe

# Linux
./napcat.sh
```

确保它正常登录 QQ 并开始运行。

### 启动 XiaoQing

```bash
cd XiaoQing
python main.py
```

看到以下日志说明启动成功：

```
2026-02-04 10:00:00 INFO - XiaoQing starting...
2026-02-04 10:00:00 INFO - Loaded plugin: core
2026-02-04 10:00:00 INFO - Loaded plugin: xiaoqing_chat
2026-02-04 10:00:00 INFO - Loaded plugin: pendo
2026-02-04 10:00:00 INFO - Loaded plugin: qingssh
2026-02-04 10:00:00 INFO - Loaded plugin: ads_paper
2026-02-04 10:00:00 INFO - Inbound server listening on 127.0.0.1:12000
```

---

## 第五步：测试

### 基础命令测试

给机器人发送命令：

```
你: /help
机器人: [帮助信息...]

你: /echo 你好世界
机器人: 你好世界

你: /plugins
机器人: [插件列表...]
```

### 智能对话测试（xiaoqing_chat）

```
你: 小青 你好
机器人: 你好！我是小青，有什么我可以帮助你的吗？

你: /xc_reset
机器人: 对话记忆已重置

你: /xc_stats
机器人: [对话统计信息...]
```

### 个人助理测试（pendo）

```
你: /todo 添加任务：完成文档更新
机器人: ✓ 已添加任务

你: /todo list
机器人: [任务列表...]

你: /note 今天天气不错
机器人: ✓ 已记录笔记
```

### SSH 远程控制测试（qingssh）

```
你: /ssh_add server1 192.168.1.100 root password
机器人: ✓ 已添加 SSH 服务器 server1

你: /ssh_list
机器人: [服务器列表...]

你: /ssh_exec server1 uptime
机器人: [服务器执行结果...]
```

---

## 常见问题

### Q: 启动后没有任何响应

**检查清单**：

1. **OneBot 是否正常运行？**
   - 查看 OneBot 的日志
   - 确认 QQ 已登录

2. **网络配置是否正确？**
   - XiaoQing 的 `inbound_http_base` 端口与 OneBot 推送端口是否一致
   - Token 是否匹配

3. **查看 XiaoQing 日志**：
   ```bash
   cat logs/xiaoqing.log
   ```

### Q: 群聊不响应

群聊默认需要满足以下条件之一：

1. 消息以命令前缀开头（如 `/help`）
2. 消息包含机器人名称（如 `小青 你好`）
3. 随机触发（默认 5% 概率）

如果想让机器人响应所有群消息：
```json
{
  "require_bot_name_in_group": false
}
```

### Q: 如何切换闲聊模式？

XiaoQing 支持两种闲聊模式：

1. **xiaoqing_chat**（推荐）：基于 LLM 的智能对话
   - 需要在 `config/secrets.json` 中配置 LLM API
   - 支持长期记忆、表情学习、情绪系统

2. **smalltalk**：基于规则的简单闲聊
   - 无需额外配置
   - 回复简单、固定

切换方法：
```json
{
  "plugins": {
    "smalltalk_provider": "xiaoqing_chat"
  }
}
```

### Q: xiaoqing_chat 不回复？

检查以下几点：

1. **是否配置了 LLM API？**
   ```json
   {
     "plugins": {
       "xiaoqing_chat": {
         "api_key": "your-api-key",
         "base_url": "https://api.openai.com/v1",
         "model": "gpt-4o-mini"
       }
     }
   }
   ```

2. **查看日志确认错误**
   ```bash
   cat logs/xiaoqing.log | grep xiaoqing_chat
   ```

3. **xiaoqing_chat 的回复频率由插件内部控制**，不是所有消息都会回复

### Q: 如何查看详细日志？

修改 `config.json`：
```json
{
  "log_level": "DEBUG"
}
```

然后重启 XiaoQing。

### Q: 端口被占用

更换端口：
```json
{
  "inbound_http_base": "http://127.0.0.1:12001",
  "inbound_ws_uri": "ws://127.0.0.1:12001/ws"
}
```

同时更新 OneBot 的配置。

---

## 下一步

- 想了解系统架构？→ [02-architecture.md](02-architecture.md)
- 想开发插件？→ [03-plugin-development.md](03-plugin-development.md)
- 想了解配置详情？→ [06-configuration.md](06-configuration.md)
- 想了解消息处理流程？→ [08-message-flow.md](08-message-flow.md)
