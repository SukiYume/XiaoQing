# 🚀 01 - 快速开始

本章带你完成 XiaoQing 的安装、配置和首次运行。

> [!TIP]
> [00-overview.md](00-overview.md) 提供项目整体概念。第一次接触项目时，先看一遍会更顺。

---

## 🖥️ 环境要求

- **Python 3.10+**（推荐 3.11）
- **OneBot 实现**：用于连接 QQ
  - 推荐 [NapCatQQ](https://github.com/NapNeko/NapCatQQ) (Modern OneBot 11 Implementation)

---

## 📦 第一步：安装依赖

```bash
git clone https://github.com/SukiYume/XiaoQing.git
cd XiaoQing
pip install -r requirements.txt
```

核心依赖包括以下组件。
- `aiohttp` - 异步 HTTP
- `websockets` - WebSocket 通信
- `apscheduler` - 定时任务
- `fastapi` + `uvicorn` - pendo Web 控制台服务器
- `PyJWT` + `passlib[bcrypt]` - pendo Web 控制台身份验证

---

## ⚙️ 第二步：配置文件

XiaoQing 使用两个配置文件：

建议先从模板复制，再按需修改：

```bash
cp config/config.json.example config/config.json
cp config/secrets.json.example config/secrets.json
```

Windows PowerShell 操作方式如下。

```powershell
Copy-Item config/config.json.example config/config.json
Copy-Item config/secrets.json.example config/secrets.json
```

### config/config.json - 基础配置

```json
{
  "bot_name": "小青",
  "command_prefixes": ["/"],
  "require_bot_name_in_group": true,
  
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
| `onebot_http_base` | OneBot 的 HTTP API 地址 | 根据你的 OneBot 配置；`xiaoqing_chat` 回收 NapCat `mface` 真实图片也依赖它 |
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
| `onebot_token` | OneBot HTTP API 的鉴权 token；启用 token 时需要同步填写，否则图片回收接口会失败 |
| `admin_user_ids` | 管理员 QQ 号列表，可执行管理命令 |
| `plugins` | 各插件的私有配置（如 API Key） |

> [!WARNING]
> `secrets.json` 包含敏感信息（token、管理员 ID、API Key），**不要提交到 Git！** 项目 `.gitignore` 已默认排除此文件。

---

## 🔗 第三步：配置 OneBot

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

### 接入模式选择

| 模式 | 优点 | 缺点 |
|------|------|------|
| 被动接收 | 简单可靠，支持响应返回 | 需要 XiaoQing 监听端口 |
| 主动连接 | 不需要 XiaoQing 监听 | 需要 OneBot 启用 WS |

被动接收模式配置较简单，也便于调试。

---

## 🟢 第四步：启动

### 启动 OneBot

先启动 NapCat。

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
2026-02-04 10:00:00 INFO - Loaded plugin: bot_core
2026-02-04 10:00:00 INFO - Loaded plugin: xiaoqing_chat
2026-02-04 10:00:00 INFO - Loaded plugin: pendo
2026-02-04 10:00:00 INFO - Loaded plugin: qingssh
2026-02-04 10:00:00 INFO - Loaded plugin: ads_paper
2026-02-04 10:00:00 INFO - Inbound server listening on 127.0.0.1:12000
```

---

## 🧪 第五步：测试

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
机器人: 诶，在呢，怎么啦

你: /xc 清空
机器人: 对话记忆已重置

你: /xc 统计
机器人: [对话统计信息...]
```

配置 `xiaoqing_chat.vision` 后，建议再补两条多模态冒烟。

```
你: [发一张普通图片]
机器人: [围绕图片内容自然接话]

你: [发一个 QQ 表情 / NapCat 收藏表情]
机器人: [把表情内容纳入对话，必要时可能顺手带一个本地表情包或 QQ 表情]
```

再补几个行为判断。

- 在群里直接 `@` 小青，或发送 `小青 + 内容`，应走 forced 回复路径。
- 只发送 `小青` 后，同一用户短时间内继续追问，小青应能接住后续问题。
- reply 引用小青上一条消息时，应按 reply-to-bot 处理。
- 最近上下文明确在聊小青时，发送 `不@她能不能听见啊` 这类“她/ta”共指消息，应能被识别为在叫小青。
- 纯普通群聊闲聊不保证每条都回复，它会经过普通插话概率、heartflow、planner 和硬频控。

这些行为属于 `xiaoqing_chat` 插件内部的 attention gate 和 participation gate，不由全局 `random_reply_rate` 决定。

### 个人助理测试（pendo）

```
你: /pendo todo add 整理项目资料 cat:工作 p:2
机器人: ✓ 已添加任务

你: /pendo todo list
机器人: [任务列表...]

你: /pendo note add 今天天气不错 #随手记
机器人: ✓ 已记录笔记

你: /pendo ledger add 35 午饭 cat:餐饮 account:微信
机器人: ✓ 已记录支出
```

如果不想在一条消息里写完金额、描述、分类和账户，也可以只发 `/pendo ledger add` 进入交互式记账。前两步需要手动输入金额和描述，后续类型、账户和分类可按数字选择。

需要浏览器界面时，继续验证 Web 控制台。

```text
你: /pendo web start
机器人: [Web 服务状态和访问地址]

你: /pendo web token
机器人: [登录 token]
```

Pendo Web 覆盖 Dashboard、Events、Tasks、Ledger、Notes、Diary、Search、Stats、Settings 和 Transfer。聊天端适合快速录入，Web 端适合集中编辑、统计和数据迁移。

### Codex 后台任务测试

Codex 插件不占用框架的多轮 session。先创建一个带标签的 Codex 会话，再把任务发到对应标签；同一标签内任务串行执行，不同标签可以并行执行，完成后由插件主动回发文字和图片结果。

```
你: /codex create main
机器人: 已创建 Codex 会话 `main`

你: /codex main 看一下当前项目结构
机器人: 已收到 Codex 任务: `main` #1

你: /codex status main
机器人: [当前运行与排队状态...]
```

默认工作目录是 `C:/Users/testuser/Desktop/XiaoQing/XiaoQing_Codex`。在 QQ 里建议统一使用 `/` 斜杠输入路径，例如 `/codex create demo cwd:C:/Users/testuser/Desktop/project`，插件会按 bot 所在系统解析。Codex 任务如果生成图片，插件会自动提供任务级 artifacts 目录，完成后把文字和图片一起回发到 QQ。

### Shell 路径测试

Shell 插件直接执行白名单命令，不经过系统 shell。Windows 下 `copy`、`del` 这类内建命令不能直接执行；复制文件优先使用 `cp`、`xcopy`、`robocopy`，或显式通过 `cmd /c copy`。

```
你: /shell cp C:/Users/testuser/Desktop/a.txt C:/Users/testuser/Desktop/b.txt
机器人: [执行结果...]

你: /shell cmd /c copy C:/Users/testuser/Desktop/a.txt C:/Users/testuser/Desktop/b.txt
机器人: [执行结果...]
```

### SSH 远程控制测试（qingssh）

```
你: /ssh添加 server1 192.168.1.100 22 root
机器人: ✓ 已添加 SSH 服务器 server1

你: /ssh列表
机器人: [服务器列表...]

你: /ssh server1
机器人: 已连接 server1，进入交互模式

你: uptime
机器人: [服务器执行结果...]
```

### pendo Web 控制台测试

pendo 插件内置了一个基于 FastAPI 的 Web 控制台，可以在浏览器中可视化管理日程、待办、笔记、日记、账本、搜索、统计和数据迁移。

```
你: /pendo web start
机器人: ✓ Web 控制台已启动，访问 http://127.0.0.1:12001

你: /pendo web status
机器人: 运行中 | 地址：http://127.0.0.1:12001
```

打开浏览器访问后，先执行 `/pendo web token` 获取登录令牌，再将令牌粘贴到登录页即可。iPhone Scriptable 小组件使用 `/pendo web widget-token` 生成只读 token。

Windows 上遇到端口绑定失败，且 `netstat -ano` 看不到默认端口被占用时，优先尝试在启动前设置 `$env:PENDO_WEB_PORT="12003"` 后重启主进程。

如需使用 nginx 反向代理部署在子路径（如 `/pendo`），参见 [06-configuration.md](06-configuration.md) 中的 nginx 配置示例。

---

## ❓ 常见排障

### 启动后没有任何响应

排查时依次检查以下项。

1. **OneBot 运行状态**
   - 查看 OneBot 的日志
   - 确认 QQ 已登录

2. **网络配置**
   - XiaoQing 的 `inbound_http_base` 端口与 OneBot 推送端口是否一致
   - Token 是否匹配

3. **查看 XiaoQing 日志**
   ```bash
   tail -n 100 logs/xiaoqing.log
   ```

   ```powershell
   Get-Content logs/xiaoqing.log -Tail 100
   ```

### 群聊不响应

群聊默认需要满足以下条件之一：

1. 消息以命令前缀开头（如 `/help`）
2. 消息包含机器人名称（如 `小青 你好`）
3. 消息 @ 机器人
4. 该用户在当前群里有活跃会话

响应所有群消息需要关闭群聊名称限制。
```json
{
  "require_bot_name_in_group": false
}
```

### 闲聊模式切换

XiaoQing 支持两种闲聊模式。

1. **xiaoqing_chat**（推荐）：基于 LLM 的智能对话
   - 需要在 `config/secrets.json` 中配置 LLM API
   - 支持长期记忆、表情学习、情绪系统

2. **smalltalk**：基于规则的简单闲聊
   - 无需额外配置
   - 回复简单、固定

通过配置切换。
```json
{
  "plugins": {
    "smalltalk_provider": "xiaoqing_chat"
  }
}
```

### xiaoqing_chat 回复缺失

排查时依次检查以下项。

1. **LLM API 配置**
   ```json
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

2. **查看日志确认错误**
   ```bash
   grep xiaoqing_chat logs/xiaoqing.log
   ```

   ```powershell
   Select-String -Path logs/xiaoqing.log -Pattern xiaoqing_chat
   ```

3. **普通群消息的回复频率由插件内部控制**，不是所有消息都会回复；但 `/xc`、`@` 机器人、直接叫 `bot_name` 都属于强制回复路径
4. **图片或表情包不回复时**，检查 `config/secrets.json -> plugins.xiaoqing_chat.vision` 是否已配置，以及日志中是否出现 `media.analyze.skip` / `media.analyze.fail`

### 详细日志查看

修改 `config.json`。
```json
{
  "log_level": "DEBUG"
}
```

然后重启 XiaoQing。

### 端口被占用

更换端口。
```json
{
  "inbound_http_base": "http://127.0.0.1:12002",
  "inbound_ws_uri": "ws://127.0.0.1:12002/ws"
}
```

同时更新 OneBot 的配置。

---

## ➡️ 下一步

- 系统架构见 [02-architecture.md](02-architecture.md)
- 插件开发见 [03-plugin-development.md](03-plugin-development.md)
- 配置详情见 [06-configuration.md](06-configuration.md)
- 消息处理流程见 [08-message-flow.md](08-message-flow.md)
