# XiaoQing

基于 **OneBot 协议**、**Python 异步运行时** 和 **插件系统** 的 QQ 机器人框架。
项目既可以作为一个轻量机器人宿主使用，也可以作为个人信息管理与自动化平台扩展，当前内置了 `xiaoqing_chat`、`pendo`、`qingssh`、`jupyter`、`qingpet` 等多类插件。

## 亮点

- **异步优先**：核心消息处理、HTTP、调度都围绕 `asyncio` 构建。
- **插件化架构**：每个插件独立目录、独立配置、独立数据目录，支持热重载。
- **命令 + 闲聊双通路**：既支持 `/command` 风格命令，也支持自然对话和 URL 自动解析。
- **多轮会话**：框架内建会话管理，适合游戏、表单、分步输入等交互。
- **定时任务**：插件可在 `plugin.json` 中直接声明调度任务。
- **Pendo Web 控制台**：个人管理插件 `pendo` 提供完整 Web UI，覆盖总览、日程、待办、账本、笔记、日记、搜索、统计、设置。
- **面向真实部署**：支持 OneBot 被动推送和 WebSocket 主动连接两种模式。

## 当前项目里值得先看的两个模块

- **[xiaoqing_chat](plugins/xiaoqing_chat)**
  面向聊天体验的主插件，包含记忆、回复检查、行为规划、表达系统等能力。

- **[pendo](plugins/pendo)**
  个人时间与信息管理中枢，聊天端和 Web 端共用一套数据模型。

## 文档入口

- [开发文档总览](docs/README.md)
- [快速开始](docs/01-getting-started.md)
- [核心架构](docs/02-architecture.md)
- [插件开发指南](docs/03-plugin-development.md)
- [消息流程](docs/08-message-flow.md)
- [内置插件列表](docs/09-plugins.md)

---

## 快速开始

### 1. 环境要求

- Python `3.10+`
- 一个可用的 OneBot 实现
  推荐 [NapCatQQ](https://github.com/NapNeko/NapCatQQ)

### 2. 安装依赖

```bash
cd XiaoQing
pip install -r requirements.txt
```

如果你需要 `pendo` 的 Web UI，`requirements.txt` 已经包含 `fastapi`、`uvicorn`、`PyJWT`、`passlib[bcrypt]` 等依赖。

### 3. 配置

项目默认读取：

- [config/config.json](config/config.json)
- `config/secrets.json`

一个最小可运行的配置大致如下：

```json
{
  "bot_name": "小青",
  "command_prefixes": ["/"],
  "require_bot_name_in_group": true,
  "random_reply_rate": 0.05,
  "enable_ws_client": false,
  "enable_inbound_server": true,
  "onebot_http_base": "http://127.0.0.1:11001",
  "onebot_ws_uri": "ws://127.0.0.1:11000/ws",
  "inbound_http_base": "http://127.0.0.1:12000",
  "inbound_ws_uri": "ws://127.0.0.1:12000/ws",
  "timezone": "Asia/Shanghai",
  "log_level": "INFO",
  "plugins": {
    "smalltalk_provider": "xiaoqing_chat"
  }
}
```

`config/secrets.json` 至少应包含：

```json
{
  "inbound_token": "your-secret-token",
  "admin_user_ids": [123456789],
  "plugins": {}
}
```

### 4. 连接 OneBot

#### 推荐：被动模式

由 OneBot 主动把消息推送给 XiaoQing：

```yaml
http:
  post:
    - url: http://127.0.0.1:12000/event
      secret: your-secret-token
```

#### 可选：主动 WebSocket 模式

由 XiaoQing 主动连接 OneBot：

```json
{
  "enable_ws_client": true,
  "enable_inbound_server": false,
  "onebot_ws_uri": "ws://127.0.0.1:11000/ws"
}
```

### 5. 启动

```bash
python main.py
```

正常启动后，日志中通常会看到：

```text
Loaded plugin bot_core
Loaded plugin xiaoqing_chat
Inbound server started ...
```

---

## 消息处理模型

### 私聊

私聊消息默认都会进入框架处理。

### 群聊

群消息满足下列任一条件时会进入命令或闲聊流程：

1. 以命令前缀开头，如 `/help`
2. 包含机器人名字，如 `小青 你好`
3. 命中随机回复概率 `random_reply_rate`

### 常用内置命令

| 命令 | 说明 |
|------|------|
| `/help` | 查看命令帮助 |
| `/plugins` | 查看已加载插件 |
| `/reload <name>` | 重载指定插件 |
| `/闭嘴 [分钟/1h]` | 群内静音机器人 |
| `/说话` | 解除静音 |
| `/metrics` | 查看运行指标 |

---

## 常用插件

### xiaoqing_chat

| 命令 | 说明 |
|------|------|
| `/xc <内容>` | 智能对话 |
| `/xc help` | 查看聊天插件帮助 |
| `/xc reset` | 清空当前会话 |
| `/xc stats` | 查看聊天状态 |

### pendo

| 命令 | 说明 |
|------|------|
| `/pendo event add 明天9点开会` | 添加日程 |
| `/pendo todo add 写报告 cat:工作 p:2` | 添加待办 |
| `/pendo note add 记录想法 #工作` | 添加笔记 |
| `/pendo diary` | 进入日记模板流程 |
| `/pendo search 关键词` | 跨模块搜索 |
| `/pendo web start` | 启动 Pendo Web 控制台 |
| `/pendo web token` | 获取 Web 登录令牌 |

### 其他内置插件

- `qingssh`：远程 SSH 管理
- `jupyter`：执行 Python / Notebook 相关操作
- `qingpet`：群宠物系统
- `apod`：每日天文图
- `astro_tools`：天文计算工具
- `ads_paper` / `arxiv_filter`：论文相关工具

---

## Pendo Web 控制台

Pendo 不只是聊天命令，它现在也是项目里最完整的浏览器控制台。

### 启动与访问

```bash
/pendo web start
/pendo web start port=9000
/pendo web status
/pendo web stop
/pendo web token
```

### 登录方式

Pendo Web 不是账号密码登录，而是 **Token 登录**。

基本流程：

1. 在聊天里执行 `/pendo web start`
2. 执行 `/pendo web token`
3. 打开浏览器访问返回的地址
4. 在登录页粘贴 Token 进入

### 当前 Web 页面

- 总览
- 日程
- 待办
- 账本
- 笔记
- 日记
- 搜索
- 统计
- 设置

当前 Web UI 使用 **FastAPI + 原生 JS + CSS**，图表主要是原生 SVG 和定制组件，不依赖传统后台模板。

---

## 插件开发

一个插件最少需要两个文件：

- `plugin.json`
- `main.py`

### 最小插件示例

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

### 可选生命周期

```python
def init(context=None) -> None:
    pass


async def shutdown(context) -> None:
    pass
```

### 会话处理

如果插件需要多轮交互，可以实现：

```python
async def handle_session(text: str, event: dict, context, session):
    ...
```

### URL 解析

如果插件希望自动处理消息中的 URL，可以实现：

```python
async def handle_url(url: str, event: dict, context):
    ...
```

### 闲聊入口

如果插件希望参与普通文本处理，可以实现：

```python
async def handle_smalltalk(text: str, event: dict, context):
    ...
```

### 定时任务

可直接在 `plugin.json` 中声明：

```json
{
  "schedule": [
    {
      "id": "morning",
      "handler": "send_morning_msg",
      "cron": {
        "hour": 8,
        "minute": 0
      },
      "group_ids": [123456789]
    }
  ]
}
```

对应函数：

```python
async def send_morning_msg(context):
    return [{"type": "text", "data": {"text": "早上好"}}]
```

更完整的开发说明请看 [插件开发指南](docs/03-plugin-development.md)。

---

## 项目结构

```text
XiaoQing/
├── main.py
├── requirements.txt
├── config/
│   ├── config.json
│   └── secrets.json
├── core/
│   ├── app.py
│   ├── dispatcher.py
│   ├── router.py
│   ├── plugin_manager.py
│   ├── session.py
│   ├── scheduler.py
│   ├── context.py
│   └── ...
├── plugins/
│   ├── bot_core/
│   ├── xiaoqing_chat/
│   ├── pendo/
│   ├── qingpet/
│   ├── qingssh/
│   ├── jupyter/
│   └── ...
├── docs/
├── tests/
└── logs/
```

---

## 测试

```bash
pytest
```

运行单个测试：

```bash
pytest tests/plugins/test_pendo.py
pytest tests/plugins/test_xiaoqing_chat.py
```

---

## 常见问题

### 群聊不响应

优先检查：

1. 消息是否以命令前缀开头
2. 是否包含机器人名称
3. 是否被静音
4. 日志里是否有对应请求记录

### Pendo Web 打不开

优先检查：

1. 是否执行了 `/pendo web start`
2. 当前环境是否安装了 `fastapi` 和 `uvicorn`
3. 端口是否被占用
4. 是否通过 `/pendo web token` 获取了有效登录令牌

### 插件热重载不生效

可以先用：

```bash
/reload 插件名
```

如果插件内部维护了复杂状态，直接重启进程通常更稳。

### Windows 上 torch 导入冲突

项目在 [main.py](main.py) 中已经提前处理了一层：

- 先设置 `KMP_DUPLICATE_LIB_OK=TRUE`
- 再尽早尝试导入 `torch`

如果仍有问题，优先检查 PyTorch、Python、VC Runtime 的版本匹配。

---

## 许可证

MIT License

---

如果你准备开始二次开发，推荐的阅读顺序是：

1. [快速开始](docs/01-getting-started.md)
2. [核心架构](docs/02-architecture.md)
3. [插件开发指南](docs/03-plugin-development.md)
