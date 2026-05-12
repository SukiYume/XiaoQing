<div align="center">

# XiaoQingBot

**基于 OneBot v11 和 Python asyncio 的 QQ 机器人框架**

[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?style=flat-square&logo=python&logoColor=white)](https://python.org)
[![License](https://img.shields.io/badge/License-MIT-green?style=flat-square)](LICENSE)
[![OneBot](https://img.shields.io/badge/OneBot-v11-black?style=flat-square)](https://onebot.dev/)
[![asyncio](https://img.shields.io/badge/async-native-blue?style=flat-square&logo=python&logoColor=white)](https://docs.python.org/3/library/asyncio.html)

`QQ / OneBot 事件 -> XiaoQing 核心框架 -> 插件路由与会话 -> OneBot 消息段回复`

</div>

## 项目定位

XiaoQingBot 是一个面向真实 QQ 使用场景的机器人项目。它既是插件化 bot 框架，也是一套可以直接长期运行的机器人应用。核心框架负责 OneBot 接入、消息解析、命令路由、会话管理、调度任务、配置热重载、插件生命周期和运行指标；插件负责具体业务能力，例如拟人聊天、个人时间管理、天文工具、远程 SSH、受限终端执行、Codex 后台任务、宠物养成、链接解析和自动签到。

项目主要由两个核心插件承担日常使用场景。

- `xiaoqing_chat`: 多模态拟人聊天插件。它能作为全局 `smalltalk_provider` 参与群聊，支持文本、图片、QQ face、NapCat mface、reply 引用、本地表情包/图片回复、记忆、PFC 行为规划、reply checker 和表达学习。
- `pendo`: 个人时间与信息管理中枢。它用同一套数据模型管理日程、待办、笔记、日记、账本、提醒、搜索、统计和迁移，同时提供聊天命令、Web 控制台和 iPhone Scriptable 小组件。

项目定位为可长期运行的 QQ 助手。它可以在群里聊天，可以私聊处理个人事务，可以定时推送提醒，可以通过 Web 管理结构化数据，也可以加载独立工具类插件处理特定需求。日常使用时，它更像一套常驻的小工具箱，而不是只响应单条命令的脚手架。

## 核心能力

| 能力 | 说明 |
|---|---|
| OneBot v11 接入 | 支持被动 HTTP inbound server 和主动 WebSocket client 两种接入方式 |
| 异步运行时 | 核心收发、调度、HTTP、插件处理都围绕 `asyncio` 设计 |
| 插件系统 | 每个插件独立目录、独立 `plugin.json`、独立数据目录和生命周期 |
| 命令路由 | 支持多个命令前缀、触发词、管理员命令、bot name 前缀剥离和参数解析 |
| Handler 链 | `BotNameHandler`、`CommandHandler`、`SessionHandler`、`SmalltalkHandler` 分层处理 |
| 多轮会话 | 内置 session manager，适合游戏、表单、REPL、SSH、记账引导等交互 |
| 后台任务队列 | 插件可自建独立队列并通过 `context.send_action()` 主动回发文字或图片结果，适合 Codex 这类长任务 |
| Smalltalk Provider | 可把普通闲聊交给 `smalltalk` 或 `xiaoqing_chat` 插件处理 |
| 调度任务 | 插件可在 `plugin.json` 中声明 cron schedule，由框架统一调度 |
| 配置热重载 | `/reload config` 可重读配置；插件可按需开启文件 watcher |
| 静音控制 | 群内 `/闭嘴` 和 `/说话` 控制机器人是否参与普通回复 |
| 运行指标 | `/metrics` 查看消息处理、插件调用、错误等指标 |
| Web 插件能力 | 插件可以自带 Web 服务，Pendo 使用 FastAPI + 原生 JS SPA |

## 代码结构

```text
XiaoQing/
├── main.py                         # 进程入口
├── config/
│   ├── config.json.example          # 基础配置示例
│   └── secrets.json.example         # 敏感配置示例
├── core/
│   ├── app.py                       # XiaoQingApp，管理核心组件生命周期
│   ├── dispatcher.py                # 消息分发和 Handler 链
│   ├── router.py                    # 命令路由
│   ├── plugin_manager.py            # 插件加载、热重载、生命周期
│   ├── session.py                   # 多轮会话
│   ├── scheduler.py                 # 插件定时任务
│   ├── onebot.py                    # OneBot HTTP / WebSocket 发送与连接
│   ├── server.py                    # inbound HTTP server
│   ├── context.py                   # PluginContext
│   ├── plugin_base.py               # 消息段和插件工具函数
│   ├── message.py                   # OneBot 消息解析
│   ├── config.py                    # 配置读取和运行时更新
│   └── metrics.py                   # 运行指标
├── plugins/
│   ├── bot_core/                    # 核心管理命令
│   ├── xiaoqing_chat/               # 拟人聊天插件
│   ├── pendo/                       # 个人时间与信息管理中枢
│   ├── codex/                       # Codex 后台会话与任务队列
│   ├── shell/                       # 管理员终端命令执行
│   └── ...                          # 其它内置插件
├── docs/                            # 项目手册
└── tests/                           # 自动化测试
```

## 消息处理概览

一条 OneBot 消息进入 XiaoQing 后，核心流程如下。

1. `InboundServer` 或 `OneBotWsClient` 收到事件。
2. `XiaoQingApp` 将事件交给 `Dispatcher`。
3. `Dispatcher` 解析消息类型、文本、用户、群号和原始消息段。
4. 全局 URL 监听、静音状态、命令前缀、bot name 和随机闲聊配置参与决策。
5. Handler 链按顺序尝试处理。
   - `BotNameHandler`: 用户只喊机器人名字时的回应。
   - `CommandHandler`: 命令路由命中插件。
   - `SessionHandler`: 活跃多轮会话继续处理。
   - `SmalltalkHandler`: 普通闲聊交给 smalltalk provider。
6. 插件返回文本、图片、语音、QQ face 等 OneBot 消息段。
7. `onebot.py` 通过 HTTP API 或 WebSocket 发送回复。

当 `plugins.smalltalk_provider = "xiaoqing_chat"` 时，群聊消息都会进入 `xiaoqing_chat` 自己的观察和回复判断。全局 `random_reply_rate` 不再决定是否进入插件；插件内部使用 attention gate、频率控制、PFC planner、主 LLM 和 reply checker 决定是否回复。

## 快速开始

### 环境要求

- Python `3.10+`，推荐 `3.11`。
- 一个 OneBot v11 实现，推荐 [NapCatQQ](https://github.com/NapNeko/NapCatQQ)。
- 如果使用 `pendo` Web 控制台，根目录 `requirements.txt` 已包含 FastAPI、uvicorn、PyJWT、passlib 等依赖。
- 如果使用 `xiaoqing_chat`，需要在 `config/secrets.json` 配置 OpenAI-compatible 聊天模型 provider；图片理解还需要视觉 provider。

### 安装依赖

```bash
git clone https://github.com/SukiYume/XiaoQing.git
cd XiaoQing
pip install -r requirements.txt
```

Windows PowerShell 下同样可以直接执行。

```powershell
pip install -r requirements.txt
```

### 创建配置

```bash
cp config/config.json.example config/config.json
cp config/secrets.json.example config/secrets.json
```

PowerShell 命令如下。

```powershell
Copy-Item config/config.json.example config/config.json
Copy-Item config/secrets.json.example config/secrets.json
```

最小 `config/config.json` 示例内容如下。

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
  "timezone": "Asia/Shanghai",
  "log_level": "INFO",
  "plugins": {
    "smalltalk_provider": "xiaoqing_chat"
  }
}
```

最小 `config/secrets.json` 示例内容如下。

```json
{
  "inbound_token": "your-secret-token",
  "admin_user_ids": [123456789],
  "plugins": {
    "xiaoqing_chat": {
      "default": "deepseek",
      "providers": {
        "deepseek": {
          "api_base": "https://api.deepseek.com",
          "api_key": "sk-xxx",
          "model": "deepseek-chat",
          "endpoint_path": "/v1/chat/completions",
          "proxy": ""
        }
      }
    }
  }
}
```

`config/secrets.json` 包含 token、管理员 QQ、LLM API Key、第三方服务密钥等敏感信息，不要提交到 Git。

### 连接 OneBot

推荐使用被动接收模式：OneBot 将事件推送到 XiaoQing 的 inbound server。

NapCat HTTP POST 示例内容如下。

```yaml
http:
  post:
    - url: http://127.0.0.1:12000/event
      secret: your-secret-token
```

也可以使用主动 WebSocket 模式，由 XiaoQing 连接 OneBot。

```json
{
  "enable_ws_client": true,
  "enable_inbound_server": false,
  "onebot_ws_uri": "ws://127.0.0.1:11000/ws"
}
```

### 启动

```bash
python main.py
```

启动成功后，日志会显示已加载插件和 inbound server / WebSocket 状态。基础验证可以在 QQ 中发送以下消息。

```text
/help
/plugins
/metrics
小青 你好
/xc 你好
/pendo
```

## 常用内置命令

| 命令 | 说明 |
|---|---|
| `/help [关键词]` | 查看命令帮助或搜索命令 |
| `/plugins` | 查看已加载插件 |
| `/reload` | 管理员热重载配置和插件 |
| `/闭嘴 [分钟/1h]` | 当前群静音机器人 |
| `/说话` | 解除当前群静音 |
| `/metrics` | 查看运行指标 |
| `/xc <内容>` | 进入 xiaoqing_chat 对话 |
| `/pendo ...` | 进入 Pendo 个人管理功能 |
| `/codex ...` | 管理 Codex 后台会话、任务队列和结果回发 |
| `/shell <命令>` | 管理员执行白名单内终端命令 |

## 核心插件

### xiaoqing_chat

`xiaoqing_chat` 是项目的主聊天插件。它支持以下能力。

- 文本对话、私聊、群聊、reply 引用。
- 入站普通图片、QQ face、NapCat mface 和混合消息。
- 出站本地图片、表情包和 QQ face。
- 多轮上下文、长期记忆、人物资料、话题摘要。
- Attention gate: `/xc`、私聊、`@`、直接叫名字、只喊名字后的追问、reply-to-bot、上下文锚定的“她/ta”共指。
- 普通群聊 participation gate: 基础插话概率、硬频控、heartflow、PFC planner。
- 主 LLM 回复、reply checker、表达学习和深度对话模式。

相关文档如下。

- [plugins/xiaoqing_chat/README.md](plugins/xiaoqing_chat/README.md)
- [plugins/xiaoqing_chat/ARCHITECTURE.md](plugins/xiaoqing_chat/ARCHITECTURE.md)

### pendo

`pendo` 是个人时间与信息管理中枢。它覆盖以下能力。

- 日程：单次、重复、多节点、地点、备注、提醒和提醒确认。
- 待办：计划日期、硬截止、状态、优先级、分类、标签和提醒。
- 笔记：标题、正文、分类、标签、引用和关联条目。
- 日记：同日多篇、模板回答、心情、评分、天气、位置和收藏。
- 账本：支出、收入、转账、账户、商户、分类、金额分存储和统计；支持 `/pendo ledger add` 交互记账，也支持单行快捷记账。
- 搜索：跨模块全文搜索和结构化筛选。
- Web 控制台：Dashboard、Events、Tasks、Ledger、Notes、Diary、Search、Stats、Settings、Transfer。
- Scriptable 小组件：只读 summary API 和 iPhone 主屏脚本。

相关文档如下。

- [plugins/pendo/README.md](plugins/pendo/README.md)
- [plugins/pendo/ARCHITECTURE.md](plugins/pendo/ARCHITECTURE.md)
- [docs/pendo-scriptable-widget.md](docs/pendo-scriptable-widget.md)

## 内置插件清单

| 分类 | 插件 | 说明 |
|---|---|---|
| 核心 | `bot_core` | 帮助、重载、插件列表、静音、配置管理、metrics |
| 聊天 | `xiaoqing_chat` | 多模态拟人聊天 |
| 聊天 | `smalltalk` | 简单闲聊、问答对和语音回复 |
| 聊天 | `chat` | 基于 Coze API 的 AI 对话 |
| 个人管理 | `pendo` | 日程、待办、笔记、日记、账本、提醒、Web |
| 工具 | `choice` | 随机选择、抽奖、多选、去重 |
| 工具 | `codex` | Codex CLI 后台会话、串行队列、并行任务和图片结果透传 |
| 工具 | `color` | 中国传统色、颜色转换、恒星光谱颜色 |
| 工具 | `wolframalpha` | Wolfram Alpha 计算 |
| 工具 | `url_parser` | 链接预览解析 |
| 工具 | `shell` | 管理员终端命令执行，支持跨平台路径归一化 |
| 工具 | `qingssh` | 多轮 SSH 连接和远程命令 |
| 工具 | `jupyter` | Jupyter Python 代码执行和 REPL |
| 工具 | `voice` | Azure TTS，内部 STT 工具函数 |
| 天文 | `apod` | NASA 每日天文图 |
| 天文 | `arxiv_filter` | arXiv 论文筛选和定时推送 |
| 天文 | `ads_paper` | ADS 论文检索、摘要和 BibTeX |
| 天文 | `astro_tools` | 时间、坐标、单位、对象查询和公式速查 |
| 天文 | `dict` | 天文学词典 |
| 天文 | `chime` | CHIME FRB 重复暴监测 |
| 外部服务 | `github` | GitHub Trending |
| 外部服务 | `earthquake` | 中国地震台网微博快讯 |
| 外部服务 | `signin` | 自动签到 |
| 外部服务 | `twitter` | Twitter 图片抓取和随机发送 |
| 娱乐 | `qingpet` | QQ 群宠物养成 |
| 娱乐 | `guess_number` | 猜数字多轮游戏 |
| 娱乐 | `minecraft` | Minecraft RCON 和状态查询 |
| 示例 | `echo` | 回显示例插件 |
| 社区 | `adnmb` | A 岛匿名版客户端 |

完整命令、配置和使用示例见 [docs/09-plugins.md](docs/09-plugins.md)。

## Pendo Web 控制台

Pendo Web 由 `plugins/pendo/web/server.py` 启动，默认地址如下。

```text
http://127.0.0.1:12001
```

常用命令如下。

```text
/pendo web start
/pendo web status
/pendo web token
/pendo web widget-token
/pendo web stop
```

启动前可用环境变量调整监听地址。

```powershell
$env:PENDO_WEB_HOST="127.0.0.1"
$env:PENDO_WEB_PORT="12003"
python main.py
```

Web Token 登录不需要账号密码；执行 `/pendo web token` 后把 token 粘贴到登录页。Scriptable 小组件使用 `/pendo web widget-token` 生成的只读 token。

## 插件开发概览

最小插件结构如下。

```text
plugins/my_plugin/
├── plugin.json
└── main.py
```

`plugin.json` 描述插件名、版本、命令、触发词、入口和定时任务。

```json
{
  "name": "my_plugin",
  "version": "0.1.0",
  "description": "示例插件",
  "entry": "main.py",
  "commands": [
    {
      "name": "hello",
      "triggers": ["hello"],
      "help": "打招呼 | /hello <name>",
      "admin_only": false
    }
  ],
  "schedule": []
}
```

`main.py` 至少实现 `handle()`。

```python
from core.plugin_base import segments

async def handle(command: str, args: str, event: dict, context):
    if command == "hello":
        name = args.strip() or "world"
        return segments(f"Hello, {name}")
    return segments("unknown command")
```

复杂插件可以实现更多入口。

- `init(context)`: 初始化资源。
- `shutdown(context)` 或 `cleanup(context)`: 释放资源。
- `handle_session(text, event, context, session)`: 多轮会话。
- `handle_smalltalk(text, event, context)`: 作为闲聊 provider。
- schedule handler: 在 `plugin.json` 中声明定时任务。

详细指南见 [docs/03-plugin-development.md](docs/03-plugin-development.md) 和 [docs/05-api-reference.md](docs/05-api-reference.md)。

## 配置与热重载

配置分为两类。

- `config/config.json`: 基础运行配置，例如 bot name、命令前缀、OneBot 地址、日志级别、插件选择和并发限制。
- `config/secrets.json`: 敏感配置，例如 inbound token、管理员 QQ、LLM API Key、第三方服务 token。

常用运行时配置支持热重载。修改配置后可以执行以下命令。

```text
/reload config
```

需要重新扫描插件或命令时执行以下命令。

```text
/reload
```

插件运行时数据通常位于各自的 `plugins/<name>/data/` 目录，不应提交。Pendo 的 SQLite 数据库、xiaoqing_chat 的媒体库/记忆/表达学习状态，以及 codex 的会话索引、`session/<name>/conversation.jsonl`、图片副本和任务 artifacts 都属于本地运行时数据。

## 测试

常用验证命令如下。

```powershell
python -m compileall -q core plugins tests
python -m pytest tests -q
```

`xiaoqing_chat` 的常用验证命令如下。

```powershell
python -m pytest tests/plugins/test_xiaoqing_chat.py -q
python -m pytest tests/plugins/test_xiaoqing_chat_media.py -q
python -m pytest tests/plugins/test_reply_checker.py -q
python -m pytest tests -k "xiaoqing or reply_checker" -q
```

`pendo` 的常用验证命令如下。

```powershell
python -m compileall -q plugins/pendo
node --check plugins/pendo/web/scriptable/pendo_widget.js
$files = Get-ChildItem -LiteralPath 'tests/plugins' -Filter 'test_pendo*.py' | Sort-Object Name | ForEach-Object { $_.FullName }
python -m pytest @files tests/test_server.py
```

Windows PowerShell 不会像 bash 一样展开某些 pytest 通配符。需要先用 `Get-ChildItem` 枚举文件，再传给 pytest。

工具插件的常用验证命令如下。

```powershell
python -m pytest tests/plugins/test_codex.py tests/plugins/test_shell_plugin.py -q
```

## 文档入口

| 文档 | 内容 |
|---|---|
| [CHANGELOG.md](CHANGELOG.md) | 项目更新记录和每次维护内容 |
| [docs/README.md](docs/README.md) | 文档目录和阅读路线 |
| [docs/00-overview.md](docs/00-overview.md) | 项目总览、概念、目录结构和设计原则 |
| [docs/01-getting-started.md](docs/01-getting-started.md) | 安装、配置、OneBot 接入和基础验证 |
| [docs/02-architecture.md](docs/02-architecture.md) | 核心架构、组件关系和数据流 |
| [docs/03-plugin-development.md](docs/03-plugin-development.md) | 插件开发指南 |
| [docs/04-core-modules.md](docs/04-core-modules.md) | `core/` 模块详解 |
| [docs/05-api-reference.md](docs/05-api-reference.md) | PluginContext、消息段、会话和 API 参考 |
| [docs/06-configuration.md](docs/06-configuration.md) | 配置项、secrets、部署和热重载 |
| [docs/07-advanced.md](docs/07-advanced.md) | 多轮会话、调度、权限、Web 插件和高级模式 |
| [docs/08-message-flow.md](docs/08-message-flow.md) | 消息从接收到回复的完整链路 |
| [docs/09-plugins.md](docs/09-plugins.md) | 所有内置插件的命令、配置和示例 |

## 常见排障

多数问题可以先从连接、配置和日志三处入手。下面几类是最常见的排查入口。

**启动后没有响应**

1. 检查 OneBot 是否已运行。
2. 检查 inbound URL 是否是 `http://127.0.0.1:12000/event` 或你的自定义地址。
3. 检查 OneBot secret 是否与 `config/secrets.json` 中的 `inbound_token` 一致。
4. 查看日志是否显示 inbound server 或 WebSocket client 启动成功。

**群聊不响应**

1. 普通框架模式下，群聊通常需要命令前缀、bot name 或随机触发。
2. 使用 `xiaoqing_chat` 时，确认 `plugins.smalltalk_provider = "xiaoqing_chat"`。
3. 检查群是否被 `/闭嘴` 静音。
4. 对普通闲聊，检查 `xiaoqing_chat` 的频控和插话概率；对 `@`、bot name、reply-to-bot 和上下文共指，检查 attention gate 日志。

**Pendo Web 打不开**

1. 执行 `/pendo web status`。
2. 确认端口没有冲突，必要时设置 `PENDO_WEB_PORT`。
3. 确认依赖已安装。
4. 执行 `/pendo web token` 获取登录 token。

**LLM 插件不可用**

1. 检查 `config/secrets.json` 中 provider 的 `api_base`、`api_key`、`model` 和 `endpoint_path`。
2. 检查代理和网络。
3. 对 `xiaoqing_chat`，用 `/xc 模型` 查看当前 provider。

## 许可证

本项目使用 MIT License。详见 [LICENSE](LICENSE)。
