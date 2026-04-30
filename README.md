<div align="center">

# 🤖 XiaoQing

**基于 OneBot 协议 × Python asyncio 的 QQ 机器人框架**

[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?style=flat-square&logo=python&logoColor=white)](https://python.org)
[![License](https://img.shields.io/badge/License-MIT-green?style=flat-square)](LICENSE)
[![OneBot](https://img.shields.io/badge/OneBot-v11-black?style=flat-square)](https://onebot.dev/)
[![asyncio](https://img.shields.io/badge/async-native-blue?style=flat-square&logo=python&logoColor=white)](https://docs.python.org/3/library/asyncio.html)

*接收 QQ 消息 → 解析命令 → 调用插件处理 → 返回响应*

</div>

---

## ✨ 亮点

| | 特性 | 说明 |
|---|------|------|
| ⚡ | **异步优先** | 核心消息处理、HTTP、调度全部围绕 `asyncio` 构建 |
| 🔌 | **插件化架构** | 每个插件独立目录、独立配置、独立数据目录；支持手动 `/reload`，配置文件默认自动监控，插件文件可按需开启 watcher |
| 💬 | **命令 + 闲聊双通路** | 支持 `/command` 风格命令，也支持自然对话和 URL 自动解析 |
| 🔄 | **多轮会话** | 框架内建会话管理，适合游戏、表单、分步输入等交互 |
| ⏰ | **定时任务** | 插件可在 `plugin.json` 中直接声明调度任务 |
| 🌐 | **Pendo Web 控制台** | 个人管理插件 `pendo` 提供完整 Web UI，覆盖总览、日程、待办、账本、笔记、日记、搜索、统计、设置和数据迁移 |
| 📱 | **Pendo Scriptable 小组件** | `pendo` 提供只读 widget API、专用 widget token 和 iPhone Scriptable 脚本，可把日程 / 待办 / 财务 / 笔记放到主屏 |
| 🚀 | **面向真实部署** | 支持 OneBot 被动推送和 WebSocket 主动连接两种模式 |

---

## 🌟 核心插件

<table>
<tr>
<td width="50%">

### 🧠 xiaoqing_chat

面向聊天体验的主插件，提供文本、普通图片、QQ 表情、NapCat `mface`、本地图库图片/表情包的多模态拟人对话，内置记忆、回复检查、行为规划、表达学习与深度对话模式。

→ [查看插件目录](plugins/xiaoqing_chat)

</td>
<td width="50%">

### 📅 pendo

个人时间与信息管理中枢，聊天端和 Web 端共用一套数据模型，覆盖日程、待办、笔记、日记、账本、提醒、搜索、统计和数据迁移。

→ [查看插件目录](plugins/pendo)

</td>
</tr>
</table>

---

## 📚 文档入口

| 文档 | 说明 |
|------|------|
| [📖 开发文档总览](docs/README.md) | 所有文档的导航索引 |
| [🗺️ 项目概览](docs/00-overview.md) | 先建立对框架和插件体系的整体认识 |
| [🚀 快速开始](docs/01-getting-started.md) | 10 分钟跑起来 |
| [🏗️ 核心架构](docs/02-architecture.md) | 框架内部设计 |
| [🔌 插件开发指南](docs/03-plugin-development.md) | 从零开发插件 |
| [🔧 配置详解](docs/06-configuration.md) | 配置项、部署注意事项、示例配置 |
| [📨 消息流程](docs/08-message-flow.md) | 消息处理全链路 |
| [🧩 内置插件列表](docs/09-plugins.md) | 可直接加载的内置插件说明 |

---

## 🚀 快速开始

### 1️⃣ 环境要求

- Python `3.10+`（推荐 3.11）
- 一个可用的 OneBot 实现 → 推荐 [NapCatQQ](https://github.com/NapNeko/NapCatQQ)

### 2️⃣ 克隆并安装依赖

```bash
git clone https://github.com/SukiYume/XiaoQing.git
cd XiaoQing
pip install -r requirements.txt
```

> [!NOTE]
> 如果你需要 `pendo` 的 Web UI，`requirements.txt` 已经包含 `fastapi`、`uvicorn`、`PyJWT`、`passlib[bcrypt]` 等依赖，无需额外安装。

### 3️⃣ 配置

项目默认读取 `config/config.json` 和 `config/secrets.json`。

建议先从示例文件复制：

```bash
cp config/config.json.example config/config.json
cp config/secrets.json.example config/secrets.json
```

如果你在 Windows PowerShell 中操作：

```powershell
Copy-Item config/config.json.example config/config.json
Copy-Item config/secrets.json.example config/secrets.json
```

**最小可运行配置（config.json）：**

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

**secrets.json（至少包含）：**

```json
{
  "inbound_token": "your-secret-token",
  "admin_user_ids": [123456789],
  "plugins": {}
}
```

> [!WARNING]
> `config/secrets.json` 包含敏感信息，不要提交到 Git！

### 4️⃣ 连接 OneBot

**推荐：被动模式**（由 OneBot 主动推送消息给 XiaoQing）

```yaml
http:
  post:
    - url: http://127.0.0.1:12000/event
      secret: your-secret-token
```

**可选：主动 WebSocket 模式**（由 XiaoQing 主动连接 OneBot）

```json
{
  "enable_ws_client": true,
  "enable_inbound_server": false,
  "onebot_ws_uri": "ws://127.0.0.1:11000/ws"
}
```

### 5️⃣ 启动

```bash
python main.py
```

正常启动后，日志中会看到：

```
Loaded plugin bot_core
Loaded plugin xiaoqing_chat
Inbound server started ...
```

常用运行时配置支持热重载。修改 `config/config.json` 或 `config/secrets.json` 后，可以直接执行 `/reload config`，通常无需重启进程；`enable_ws_client`、`enable_inbound_server`、`onebot_ws_uri`、`inbound_http_base`、`inbound_ws_uri`、`max_concurrency`、`session_timeout`、`timezone` 等项都会在运行时重建或更新对应组件。

### 6️⃣ 启动前检查清单

- `config/config.json` 和 `config/secrets.json` 已存在
- OneBot 推送地址与 `inbound_http_base` 一致
- `inbound_token` 与 OneBot 配置中的密钥一致
- 如果使用 `xiaoqing_chat`，已经在 `secrets.json` 填好 LLM 配置
- 如果使用 `pendo web`，当前环境可用 `fastapi` 与 `uvicorn`

---

## 🛠️ 常用内置命令

| 命令 | 说明 |
|------|------|
| `/help` | 查看命令帮助 |
| `/plugins` | 查看已加载插件 |
| `/reload` | 重载配置与插件 |
| `/闭嘴 [分钟/1h]` | 群内静音机器人 |
| `/说话` | 解除静音 |
| `/metrics` | 查看运行指标 |

---

## 🧩 常用插件

### 🧠 xiaoqing_chat

> [!IMPORTANT]
> `xiaoqing_chat` 以 `/xc` 为统一入口，支持文本对话、图片上下文、QQ 表情参与对话、本地表情包库复用，以及可单独配置的视觉模型能力。
>
> `xiaoqing_chat` 依赖聊天 LLM，若要启用图片/表情包理解，还需要额外配置视觉模型。推荐在 `config/secrets.json` 中按 provider 结构配置：
> ```json
> {
>   "plugins": {
>     "xiaoqing_chat": {
>       "default": "deepseek",
>       "providers": {
>         "deepseek": {
>           "api_base": "https://api.deepseek.com",
>           "api_key": "sk-xxx",
>           "model": "deepseek-chat",
>           "endpoint_path": "/v1/chat/completions"
>         }
>       },
>       "vision": {
>         "default": "glm-4.6v-flash",
>         "providers": {
>           "glm-4.6v-flash": {
>             "api_base": "https://open.bigmodel.cn/api/paas/v4",
>             "api_key": "your-vision-key",
>             "model": "glm-4.6v-flash",
>             "endpoint_path": "/chat/completions",
>             "thinking": {
>               "type": "disabled"
>             }
>           }
>         }
>       }
>     }
>   }
> }
> ```

> [!TIP]
> 启用媒体能力后，`xiaoqing_chat` 可以把用户发送的普通图片、NapCat `mface`、QQ 原生 `face` 表情都纳入正常对话流；识别为表情包的图片会自动落到 `plugins/xiaoqing_chat/data/media/library/`，后续在合适语境下复用发送。
>
> 作为 `smalltalk_provider` 使用时，所有群聊消息都会进入插件；被 `@` 或直接叫机器人名字会走强制回复路径。出站图片/表情/QQ 表情由主回复 LLM 在文本里写一个 `[想发...]` marker 触发，插件再按图库或 QQ face 目录解析成实际发送段；如果图库里有旧坏条目，会在后台异步补修，不阻塞当前回复。

| 命令 | 说明 |
|------|------|
| `/xc <内容>` | 智能对话；启用媒体能力后也能围绕图片/表情包继续聊 |
| `/xc help` | 查看聊天插件帮助 |
| `/xc 清空` | 清空当前会话；兼容别名 `/xc reset` |
| `/xc 统计` | 查看聊天状态；兼容别名 `/xc stats` |
| `/xc 配置` / `/xc 记忆` / `/xc 表达` / `/xc 黑话` / `/xc 模型` | 查看配置、检索长期记忆、查看表达/黑话、查看或切换模型供应商 |

### 📅 pendo

| 命令 | 说明 |
|------|------|
| `/pendo event add 明天9点开会` | 添加日程 |
| `/pendo todo add 写报告 cat:工作 p:2` | 添加待办 |
| `/pendo note add 记录想法 #工作` | 添加笔记 |
| `/pendo ledger quick 35 午饭 cat:餐饮 account:微信` | 快速记账 |
| `/pendo diary` | 进入日记模板流程 |
| `/pendo search 关键词` | 跨模块搜索 |
| `/pendo web start` | 启动 Pendo Web 控制台 |
| `/pendo web token` | 获取 Web 登录令牌 |
| `/pendo web widget-token` | 获取 Scriptable 小组件令牌 |

> [!NOTE]
> Pendo Web 的公开 demo 会话默认关闭。只有在明确需要临时演示环境时，才通过环境变量 `PENDO_WEB_DEMO_ENABLED=1` 启用。

### 🔧 其他内置插件

| 插件 | 说明 |
|------|------|
| `qingssh` | 远程 SSH 管理 |
| `jupyter` | 执行 Python / Notebook 相关操作 |
| `qingpet` | 群宠物系统 |
| `apod` | 每日天文图 |
| `astro_tools` | 天文计算工具 |
| `ads_paper` / `arxiv_filter` | 论文相关工具 |

---

## 🌐 Pendo Web 控制台

Pendo 不只是聊天命令——它也是项目里最完整的浏览器控制台。

### 启动与访问

```bash
/pendo web start           # 启动（默认端口 8765）
/pendo web status          # 查看状态
/pendo web stop            # 停止
/pendo web token           # 获取登录令牌
/pendo web widget-token    # 获取 Scriptable 小组件令牌
```

> [!TIP]
> Pendo Web 使用 **Token 登录**，无需账号密码。执行 `/pendo web start` 启动后，再执行 `/pendo web token` 获取令牌，粘贴到登录页即可进入；如果要给 iPhone Scriptable 小组件使用，则执行 `/pendo web widget-token`。

> [!NOTE]
> 如需修改监听端口，请在启动前设置 `PENDO_WEB_PORT`（例如 PowerShell 下执行 `$env:PENDO_WEB_PORT="8766"; python main.py`）。在 Windows 上如果 `netstat -ano` 看不到 `8765` 被占用，但仍然报绑定失败，通常是系统拒绝绑定该端口，直接换端口比继续排查“谁占用了它”更有效。

### 页面一览

```
总览  ·  日程  ·  待办  ·  账本  ·  笔记  ·  日记  ·  搜索  ·  统计  ·  设置  ·  迁移
```

Web UI 基于 **FastAPI + 原生 JS + CSS** 构建，API 统一挂载在 `/api`，迁移页支持 `.pendo.zip` Bundle 的预览、导入、导出、冲突策略和审计日志。

如果你想把 Pendo 放到 iPhone 主屏：

- `/pendo web widget-token` 用来生成只读 widget token
- `plugins/pendo/web/scriptable/pendo_widget.js` 是可直接导入 Scriptable 的脚本，但仓库内只保留 `BASE_URL` / `TOKEN` 占位值，需要先替换成你自己的配置
- 小组件会显示未来 30 天内最多 5 条日程，以及右侧最多 5 条待办 / 财务 / 笔记摘要

---

## 🔌 插件开发

一个插件最少需要两个文件：`plugin.json` + `main.py`。

**plugins/myplugin/plugin.json**

```json
{
  "name": "myplugin",
  "version": "1.0.0",
  "description": "示例插件",
  "entry": "main.py",
  "commands": [
    {
      "name": "hello",
      "triggers": ["hello", "你好"],
      "help": "打个招呼",
      "admin_only": false
    }
  ],
  "schedule": []
}
```

**plugins/myplugin/main.py**

```python
from typing import Any

async def handle(command: str, args: str, event: dict[str, Any], context) -> list[dict[str, Any]]:
    name = args.strip() or "世界"
    return [{"type": "text", "data": {"text": f"你好，{name}！"}}]
```

### 可选生命周期钩子

```python
async def init(context) -> None: ...      # 插件加载时
async def shutdown(context) -> None: ...  # 插件卸载时
```

### 多轮会话

```python
async def handle_session(text: str, event: dict, context, session): ...
```

### 定时任务（在 plugin.json 中声明）

```json
{
  "schedule": [
    {
      "id": "morning",
      "handler": "send_morning_msg",
      "cron": { "hour": 8, "minute": 0 },
      "group_ids": [123456789]
    }
  ]
}
```

> [!TIP]
> 更完整的开发说明请看 [插件开发指南](docs/03-plugin-development.md)。

---

## 📁 项目结构

```text
XiaoQing/
├── main.py
├── requirements.txt
├── config/
│   ├── config.json
│   └── secrets.json          ← 不要提交！
├── core/
│   ├── app.py                ← 应用主类
│   ├── dispatcher.py         ← 消息分发
│   ├── router.py             ← 命令路由
│   ├── plugin_manager.py     ← 插件管理
│   ├── session.py            ← 多轮会话
│   ├── scheduler.py          ← 定时任务
│   ├── context.py            ← 插件上下文
│   └── ...
├── plugins/
│   ├── bot_core/
│   ├── xiaoqing_chat/        ← 智能对话
│   ├── pendo/                ← 个人管理 + Web 控制台
│   ├── qingpet/
│   ├── qingssh/
│   ├── jupyter/
│   └── ...                   ← 更多可直接加载的内置插件
├── docs/
├── tests/
└── logs/
```

---

## 🧪 测试

```bash
pytest                                          # 运行所有测试
pytest tests/plugins/test_pendo*.py tests/test_server.py  # pendo 相关测试（bash/zsh）
pytest tests/plugins/test_xiaoqing_chat.py     # 单个测试文件
pytest tests/plugins/test_xiaoqing_chat_media.py  # xiaoqing_chat 图片/表情包链路
```

PowerShell 下通配符不会按同样方式传给 pytest；需要先枚举文件或直接运行 `tests/plugins` 目录下的目标文件。

---

## ❓ 常见问题

<details>
<summary><b>群聊不响应？</b></summary>

群消息满足下列任一条件才会进入处理流程：

1. 以命令前缀开头，如 `/help`
2. 包含机器人名字，如 `小青 你好`
3. 命中随机回复概率 `random_reply_rate`

其他排查：
- 检查是否被静音（`/说话` 解除）
- 查看日志里是否有对应请求记录

</details>

<details>
<summary><b>Pendo Web 打不开？</b></summary>

优先检查：
1. 是否执行了 `/pendo web start`
2. 当前环境是否安装了 `fastapi` 和 `uvicorn`
3. `127.0.0.1:8765` 是否被占用，或是否被系统拒绝绑定（Windows 上常见）
4. 是否通过 `/pendo web token` 获取了有效登录令牌

补充：
- 如果报 `WinError 10048`，说明端口真的被其他进程占用了
- 如果报 `WinError 10013`，通常是 Windows 保留端口、虚拟化网络组件或安全策略导致，优先改 `PENDO_WEB_PORT`

</details>

<details>
<summary><b>插件热重载不生效？</b></summary>

```bash
/reload 插件名
```

默认不会自动监控插件文件。需要在 `config/config.json` 中显式开启：

```json
{
  "enable_plugin_watcher": true
}
```

如果插件内部维护了复杂状态，直接重启进程通常更稳。

</details>

<details>
<summary><b>Windows 上 torch 导入冲突？</b></summary>

项目在 `main.py` 中已经提前处理：先设置 `KMP_DUPLICATE_LIB_OK=TRUE`，再尽早尝试导入 `torch`。如果仍有问题，优先检查 PyTorch、Python、VC Runtime 的版本匹配。

</details>

---

## 📖 推荐阅读顺序

如果你准备开始二次开发：

1. [🚀 快速开始](docs/01-getting-started.md)
2. [🏗️ 核心架构](docs/02-architecture.md)
3. [🔌 插件开发指南](docs/03-plugin-development.md)

---

<div align="center">

MIT License · Built with Python asyncio · OneBot v11

</div>
