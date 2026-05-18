# 🔌 03 - 插件开发指南

本章从一个最小插件开始，说明插件结构、生命周期、消息段、会话、定时任务和常见工程实践。

> [!TIP]
> 一个插件最少只需要 `plugin.json` 和 `main.py` 两个文件。先看 [📂 插件基础](#-插件基础) 和 [💻 main.py 编写](#-mainpy-编写)，就能写出第一个可运行插件。

---

## 📂 插件基础

XiaoQing 插件有两种常见规模。

- **轻量插件**：一个 `plugin.json` 加一个 `main.py` 就能完成，例如 `echo`、`choice`、`wolframalpha`。
- **复合插件**：拥有自己的子目录、服务层、数据模型、测试和文档，例如 `pendo` 和 `xiaoqing_chat`。

无论规模大小，框架看到的入口都一样：插件目录、`plugin.json`、入口模块、`handle()`、可选生命周期钩子和可选 schedule handler。大型插件应在自己的目录下维护 `README.md` 和 `ARCHITECTURE.md`，分别说明使用方式和工程结构。

### 插件结构

每个插件应是位于 `plugins/` 目录下的 Python 包，并包含 `__init__.py`。

```
plugins/
└── myplugin/
    ├── plugin.json     # 必需：插件配置
    ├── main.py         # 必需：入口代码
    ├── __init__.py     # 推荐：使插件成为 Python 包
    ├── README.md       # 推荐：插件使用手册
    ├── ARCHITECTURE.md # 推荐：复杂插件的架构说明
    ├── config.py       # 可选：配置文件
    ├── utils.py        # 可选：工具函数
    └── data/           # 可选：数据目录（自动创建）
```

### 导入规范

从 v2.0 开始，插件被加载为标准的 Python 包 (`xiaoqing_plugins.plugin_name`)。插件内部模块使用**相对导入**。

**plugins/myplugin/main.py**:
```python
# ✅ 推荐：相对导入
from .config import DEFAULT_CONFIG
from .utils import helper_function
from . import models

# ❌ 不推荐：绝对导入（仅当模块在 sys.path 时有效，但不稳定）
# from myplugin.config import DEFAULT_CONFIG 
```

### 最小示例

**plugins/hello/plugin.json**：
```json
{
  "name": "hello",
  "version": "1.0.0",
  "entry": "main.py",
  "commands": [
    {
      "name": "hello",
      "triggers": ["hello", "你好"],
      "help": "打个招呼"
    }
  ]
}
```

**plugins/hello/main.py**：
```python
from typing import Any, Dict, List
from core.plugin_base import segments

# 如果有子模块，使用相对导入
# from . import utils

async def handle(
    command: str,
    args: str,
    event: Dict[str, Any],
    context
) -> List[Dict[str, Any]]:
    name = args.strip() or "世界"
    return segments(f"你好，{name}！")
```

**测试**：
```
用户: /hello
机器人: 你好，世界！

用户: /你好 小明
机器人: 你好，小明！
```

---

## 📋 plugin.json 配置

### 完整字段

```json
{
  "name": "myplugin",
  "version": "1.0.0",
  "description": "插件描述",
  "entry": "main.py",
  "enabled": true,
  "concurrency": "parallel",
  
  "commands": [
    {
      "name": "cmd",
      "triggers": ["cmd", "命令"],
      "help": "命令帮助文本",
      "admin_only": false,
      "priority": 0
    }
  ],
  
  "schedule": [
    {
      "id": "daily_task",
      "handler": "send_daily",
      "cron": {"hour": 8, "minute": 0},
      "group_ids": [123456789]
    }
  ]
}
```

### 字段说明

| 字段 | 类型 | 必需 | 说明 |
|------|------|------|------|
| `name` | string | ✅ | 插件唯一标识，与目录名一致 |
| `version` | string | ✅ | 版本号（语义化版本） |
| `entry` | string | ✅ | 入口文件，通常是 `main.py` |
| `description` | string | ❌ | 插件描述 |
| `enabled` | bool | ❌ | 是否启用，默认 `true` |
| `concurrency` | string | ❌ | `parallel`（默认）或 `serial` |
| `commands` | array | ❌ | 命令列表 |
| `schedule` | array | ❌ | 定时任务列表 |

### commands 字段

| 字段 | 类型 | 必需 | 说明 |
|------|------|------|------|
| `name` | string | ✅ | 命令名，传给 handle() 的 command 参数 |
| `triggers` | array | ✅ | 触发词列表 |
| `help` | string | ❌ | 帮助文本，显示在 /help 中 |
| `admin_only` | bool | ❌ | 是否仅管理员可用 |
| `priority` | int | ❌ | 优先级，越大越优先，默认 0 |

### schedule 字段

| 字段 | 类型 | 必需 | 说明 |
|------|------|------|------|
| `id` | string | ✅ | 任务 ID，全局唯一 |
| `handler` | string | ✅ | main.py 中的函数名 |
| `cron` | object | ✅ | APScheduler cron 表达式 |
| `group_ids` | array | ❌ | 发送目标群，空则用默认群 |

---

## 💻 main.py 编写

### Dispatcher 线性处理流程

Dispatcher 使用固定顺序处理消息。插件通过约定函数接入：命令用 `handle()`，多轮会话用 `handle_session()`，闲聊回落用 `handle_smalltalk()`，只喊机器人名字用 `call_bot_name_only()`。

#### 处理顺序

```
消息到达 Dispatcher
    ↓
解析 MessageContext
    ↓
URL-only 短路
    ↓
处理门控（私聊 / require_bot_name_in_group=false / has_prefix / 活跃会话）
    ↓
只喊机器人名字或只 @ 机器人
    ↓
命令匹配并调用 handle()
    ↓
未知命令提示（仅严格命令前缀）
    ↓
活跃会话并调用 handle_session()
    ↓
smalltalk 回落并调用 handle_smalltalk()
```

#### 插件与分发流程的交互

1. **命令处理**
   - 用户发送 `/your_command args`
   - router 匹配到插件命令
   - Dispatcher 调用插件的 `handle()` 函数
   - 命令返回后不会继续进入会话或闲聊回落

2. **会话处理**
   - 用户在活跃会话中发送普通消息
   - 命令未匹配时，Dispatcher 调用插件的 `handle_session()` 函数
   - 会话处理成功后不会继续进入闲聊回落

3. **闲聊处理**
   - 插件作为 `smalltalk_provider` 时
   - 只有消息通过门控、未命中命令、未被活跃会话消费、且群聊未静音时，Dispatcher 才调用 `handle_smalltalk()`
   - 插件根据上下文决定是否返回消息，返回 `[]` 表示不回复

#### 短路示例

```python
# 场景：用户在猜数字会话中，同时发送了命令
# 用户的会话状态：guess_game = True

# 执行顺序：
# 1. 命令匹配到 /guess
# 2. 调用 guess.handle()
# 3. 返回 ["游戏开始！"]
# 4. 直接返回，不进入 guess.handle_session()

# 场景：用户在会话中，但没有发送命令
# 用户的会话状态：guess_game = True

# 执行顺序：
# 1. 命令未匹配
# 2. 发现活跃会话
# 3. 调用 guess.handle_session()
# 4. 返回 ["太大了！"]
# 5. 直接返回，不进入 handle_smalltalk()
```

---

### handle() 函数

**签名**：
```python
async def handle(
    command: str,           # 命令名（plugin.json 中的 name）
    args: str,              # 命令后的参数字符串
    event: Dict[str, Any],  # 原始 OneBot 事件
    context: PluginContext  # 插件上下文
) -> List[Dict[str, Any]]:  # 返回消息段列表
```

**多命令处理**：
```python
async def handle(command: str, args: str, event: Dict, context) -> List:
    if command == "add":
        return await handle_add(args, context)
    elif command == "list":
        return await handle_list(context)
    elif command == "delete":
        return await handle_delete(args, context)
    return segments("未知命令")
```

### handle_smalltalk() 函数（可选）

作为 `smalltalk_provider` 的插件需要实现此函数，例如 `xiaoqing_chat`。

```python
async def handle_smalltalk(
    text: str,              # 用户输入的文本（已去除前缀）
    event: Dict[str, Any],  # 原始 OneBot 事件
    context                # 插件上下文
) -> List[Dict[str, Any]]:  # 返回消息段列表
    """处理闲聊消息"""
    
    # 根据上下文决定是否回复
    should_reply = await should_reply(text, event, context)
    if not should_reply:
        return []  # 不回复
    
    # 生成回复
    response = await generate_response(text, context)
    return segments(response)
```

**重要特性**

1. **智能回复控制**
   - 根据上下文判断回复时机
   - 返回 `[]` 表示不回复
   - 返回非空列表表示回复

2. **xiaoqing_chat 特殊处理**
   - 当 `smalltalk_provider` 设置为 `xiaoqing_chat` 时
   - 所有消息会先进入 `observe_message()` 供插件更新上下文
   - 只有通过 dispatcher 门控并落到 smalltalk 回落时，才会进入 `handle_smalltalk()`
   - `random_reply_rate` 不参与 dispatcher 分发
   - 由插件内部的 attention gate、硬频控、普通插话概率、PFC planner 和 reply checker 控制是否回复
   - `/xc`、私聊、`@`、直接叫名字、只喊名字后的追问、reply 引用小青、以及有近期上下文锚点的“她/ta”共指召唤会走 forced 路径

3. **与其他流程的关系**
   - `handle_smalltalk()` 是最后的回落路径
   - 命令、未知命令提示、活跃会话先于 `handle_smalltalk()` 执行
   - 群聊静音只跳过 `handle_smalltalk()`，不影响命令、URL-only、只喊名字或活跃会话

**示例：简单闲聊插件**

```python
async def handle_smalltalk(text: str, event: Dict, context) -> List:
    """简单规则闲聊"""
    
    # 问候
    if text in ["你好", "hello", "hi"]:
        return segments("你好！有什么我可以帮助你的吗？")
    
    # 询问
    if "你叫什么" in text or "名字" in text:
        bot_name = context.config.get("bot_name", "小青")
        return segments(f"我叫 {bot_name}~")
    
    # 不回复其他消息
    return []
```

**示例：智能闲聊（xiaoqing_chat 风格）**

```python
async def handle_smalltalk(text: str, event: Dict, context) -> List:
    """基于 LLM 的智能闲聊"""
    
    # 1. 检查是否应该回复
    user_id = event.get("user_id")
    if not should_reply_to_user(user_id, text):
        return []
    
    # 2. 获取历史上下文
    history = await get_conversation_history(user_id, context)
    
    # 3. 调用 LLM
    response = await call_llm(
        prompt=text,
        history=history,
        context=context
    )
    
    # 4. 保存对话历史
    await save_conversation(user_id, text, response, context)
    
    # 5. 返回回复
    return segments(response)


async def should_reply_to_user(user_id: int, text: str) -> bool:
    """判断是否应该回复"""
    # 可以实现更复杂的逻辑：
    # - 用户白名单/黑名单
    # - 消息频率控制
    # - 关键词匹配
    # - 情绪分析
    return True
```

---

### 返回值

返回 OneBot 消息段列表。使用便捷函数：

```python
from core.plugin_base import text, image, image_url, record, segments

# 纯文本（最常用）
return segments("Hello World")

# 等价于
return [{"type": "text", "data": {"text": "Hello World"}}]

# 图片
return [image_url("https://example.com/pic.jpg")]

# 本地图片
return [image("/path/to/image.png")]

# 组合消息
return [
    text("看这张图："),
    image_url("https://example.com/pic.jpg"),
    text("\n怎么样？")
]

# 语音
return [record("/path/to/audio.mp3")]

# 不回复
return []
```

---

## 🔧 PluginContext 详解

`context` 是插件的上下文对象，提供各种工具。

### 属性

```python
# 配置
context.config       # Dict - config.json 内容
context.secrets      # Dict - secrets.json 完整内容

# 路径
context.plugin_name  # str - 插件名
context.plugin_dir   # Path - 插件目录 (plugins/myplugin/)
context.data_dir     # Path - 数据目录 (plugins/myplugin/data/)

# 工具
context.logger       # Logger - 日志记录器（自动附带 request_id）
context.http_session # aiohttp.ClientSession - HTTP 客户端
context.metrics      # MetricsCollector | None - 运行指标收集器

# 当前消息上下文
context.current_user_id   # int | None
context.current_group_id  # int | None

# 插件私有状态（当次请求生命周期内有效，不跨请求持久化）
context.state        # Dict[str, Any]
```

### 常用方法

```python
# 获取默认发送群列表
groups = context.default_groups()

# 重载配置
context.reload_config()

# 重载所有插件
context.reload_plugins()

# 获取所有命令
commands = context.list_commands()

# 获取所有插件
plugins = context.list_plugins()
```

### 会话方法（多轮对话）

会话位于 dispatcher 线性流程中的命令匹配之后、smalltalk 回落之前，用于实现多轮对话。

#### 会话生命周期

```
1. 用户发送命令（如 /guess）
       │
       ▼
2. 插件调用 context.create_session()
       │
       ▼
3. 会话创建，存储初始数据
       │
       ▼
4. 用户后续消息在命令未匹配时进入会话处理
       │
       ▼
5. 调用 handle_session()，不调用 handle()
       │
       ├─ 继续对话 ──> 回到步骤 5
       │
       └─ 对话结束 ──> context.end_session()
                           │
                           ▼
                      会话被删除
```

#### Context 方法

```python
# 创建会话
session = await context.create_session(
    initial_data={"step": 1, "target": 42},
    timeout=300.0  # 超时时间（秒）
)

# 获取当前会话
session = await context.get_session()

# 结束会话
await context.end_session()

# 检查是否有会话
has = await context.has_session()
```

#### handle_session() 函数

```python
async def handle_session(
    text: str,              # 用户输入的文本
    event: Dict[str, Any],  # 原始 OneBot 事件
    context,               # 插件上下文
    session                # 会话对象
) -> List[Dict[str, Any]]:  # 返回消息段列表
    """处理会话中的消息"""
    step = session.get("step", 1)
    target = session.get("target")
    
    if step == 1:
        guess = int(text)
        if guess < target:
            session.set("step", 2)
            return segments("太小了！再试试")
        elif guess > target:
            session.set("step", 2)
            return segments("太大了！再试试")
        else:
            await context.end_session()
            return segments("恭喜你猜对了！")
    
    # ... 更多步骤
```

#### 会话对象方法

```python
# 获取数据
value = session.get("key", default=None)

# 设置数据
session.set("key", value)

# 删除数据
session.delete("key")

# 检查是否过期
is_expired = session.is_expired()

# 获取剩余时间（秒）
remaining = session.get_remaining_time()
```

#### 完整示例：猜数字游戏

```python
import random

async def handle(command: str, args: str, event: Dict, context) -> List:
    """开始游戏"""
    target = random.randint(1, 100)
    
    # 创建会话
    await context.create_session(
        initial_data={
            "target": target,
            "attempts": 0,
            "start_time": time.time()
        },
        timeout=180  # 3分钟超时
    )
    
    return segments(
        "🎮 猜数字游戏开始！\n"
        "我已经想好了一个 1-100 的数字\n"
        "请输入你的猜测（输入 '退出' 结束游戏）"
    )


async def handle_session(text: str, event: Dict, context, session) -> List:
    """处理游戏中的消息"""
    
    # 退出命令
    if text.lower() in ["退出", "quit", "q", "exit"]:
        target = session.get("target")
        await context.end_session()
        return segments(f"游戏结束，答案是 {target}")
    
    # 解析猜测
    try:
        guess = int(text.strip())
    except ValueError:
        return segments("请输入有效的数字")
    
    target = session.get("target")
    attempts = session.get("attempts", 0) + 1
    session.set("attempts", attempts)
    
    # 判断结果
    if guess < target:
        return segments(f"太小了！（{attempts} 次尝试）")
    elif guess > target:
        return segments(f"太大了！（{attempts} 次尝试）")
    else:
        elapsed = int(time.time() - session.get("start_time"))
        await context.end_session()
        return segments(
            f"🎉 恭喜你猜对了！\n"
            f"答案：{target}\n"
            f"尝试次数：{attempts}\n"
            f"用时：{elapsed} 秒"
        )
```

#### 会话注意事项

1. **会话优先级**：会话处理在命令匹配之后，但优先于闲聊
2. **超时自动清理**：超过 timeout 时间会话自动删除
3. **每个用户独立**：每个 `(user_id, group_id)` 组合有独立的会话
4. **手动结束**：游戏结束时必须调用 `context.end_session()`

#### 长任务不要滥用 Session

框架 session 适合“下一条消息就是当前流程输入”的交互，例如猜数字、表单填写、SSH 交互和 Pendo 记账引导。它不适合承载长时间运行的后台任务，因为活跃 session 会在命令未命中时抢先接管同一用户后续消息，容易影响闲聊或其他普通输入。

如果插件需要后台执行并在完成后主动通知，建议像 `codex` 插件一样在插件内部维护自己的会话标签和任务队列：

1. 用普通命令创建业务会话，例如 `/codex create main cwd:C:/project`。
2. 后续命令显式带标签，例如 `/codex main <任务>`，插件立即返回“已收到”。
3. 插件内部按标签串行、跨标签并行执行任务。
4. 任务完成后用 `context.send_action(build_action(...))` 主动发送结果。
5. 运行时状态写入 `context.data_dir`，例如 `plugins/codex/data/sessions.json`、`session/<label>/conversation.jsonl` 和任务图片 artifacts。

这种设计不会占用框架活跃会话，因此不影响同一用户继续发送其他命令或闲聊。

### 静音控制

```python
# 静音群 30 分钟
context.mute_group(group_id, 30)

# 解除静音
context.unmute_group(group_id)

# 检查是否静音
is_muted = context.is_group_muted(group_id)

# 获取剩余静音时间
remaining = context.get_mute_remaining(group_id)
```

---

## 🔍 参数解析

对于带参数的命令，`core.args` 模块提供了结构化解析：

```python
from core.args import parse

async def handle(command: str, args: str, event: Dict, context) -> List:
    # args = "add 完成报告 p:2 --cat=工作"
    parsed = parse(args)

    # 位置参数
    sub = parsed.first          # "add"
    content = parsed.rest(1)    # "完成报告 p:2"

    # 选项（支持 --key=value 和 --key value 形式）
    cat = parsed.opt("cat")     # "工作"

    # 检查选项是否存在
    if parsed.has("dry-run"):
        ...

    # 获取指定位置参数
    idx = parsed.get(2, default="")
```

**支持的参数格式**：

```
/cmd arg1 arg2 --option=value --flag -f val
              ↑ 长选项=值       ↑ 标志  ↑ 短选项+值
```

简单命令不需要 `parse()`，直接用字符串操作即可；当命令有多个可选参数或选项时，`parse()` 能避免手写分割逻辑。

---

## 💬 消息构建

### 基础函数

```python
from core.plugin_base import text, image, image_url, record, record_url, segments

# 文本
text("Hello")
# -> {"type": "text", "data": {"text": "Hello"}}

# 手写本地文件消息段时，优先使用 Path.as_uri()
from pathlib import Path

# 图片（本地文件）
image("/path/to/image.png")
# -> {"type": "image", "data": {"file": "file:///path/to/image.png"}}

# 图片（URL）
image_url("https://example.com/pic.jpg")
# -> {"type": "image", "data": {"file": "https://example.com/pic.jpg"}}

# 语音（本地文件）
record("/path/to/audio.mp3")

# 手写消息段时可直接构造 record 段：
{"type": "record", "data": {"file": Path("/path/to/audio.mp3").resolve().as_uri()}}

# 语音（URL）
record_url("https://example.com/audio.mp3")

# 自动转换
segments("Hello")        # 字符串 -> 文本消息段
segments(None)           # None -> 空列表
segments([text("Hi")])   # 列表 -> 原样返回
```

### 复杂消息示例

```python
# 带格式的文本
return segments(
    "📊 统计信息\n"
    "━━━━━━━━━━\n"
    f"用户数: {user_count}\n"
    f"消息数: {msg_count}\n"
    "━━━━━━━━━━"
)

# 多媒体消息
return [
    text("今日天气："),
    image_url(weather_image),
    text(f"\n温度: {temp}°C\n湿度: {humidity}%")
]
```

---

## 🔄 生命周期钩子

### init() - 初始化

插件加载时调用，用于初始化资源。

```python
async def init(context):
    """插件初始化"""
    context.logger.info("插件已加载")
    
    # 初始化数据文件
    data_file = context.data_dir / "data.json"
    if not data_file.exists():
        data_file.write_text("{}")
    
    # 初始化全局变量
    global db_connection
    db_connection = await connect_database()
```

### shutdown() - 清理

插件卸载时调用，用于清理资源。

> [!WARNING]
> `shutdown()` 有 **5 秒超时限制**，超时将被强制中断。避免在此处执行耗时操作，尽快保存数据并关闭连接。

```python
async def shutdown(context):
    """插件卸载"""
    context.logger.info("插件正在卸载...")
    
    # 保存数据
    await save_data()
    
    # 关闭连接
    global db_connection
    if db_connection:
        await db_connection.close()
```

---

## 🌐 HTTP 请求

使用 `context.http_session`（aiohttp.ClientSession）：

```python
async def handle(command: str, args: str, event: Dict, context) -> List:
    # GET 请求
    async with context.http_session.get("https://api.example.com/data") as resp:
        if resp.status == 200:
            data = await resp.json()
        else:
            return segments(f"请求失败: {resp.status}")
    
    # POST 请求
    async with context.http_session.post(
        "https://api.example.com/submit",
        json={"key": "value"},
        headers={"Authorization": "Bearer token"}
    ) as resp:
        result = await resp.json()
    
    return segments(f"结果: {result}")
```

### 处理同步库

某些库（如 `requests`）是同步的，需要在线程池中运行：

```python
from core.plugin_base import run_sync
import requests

async def handle(command: str, args: str, event: Dict, context) -> List:
    # 在线程池中运行同步代码
    response = await run_sync(requests.get, "https://api.example.com")
    return segments(response.text)
```

---

## 💾 数据持久化

### 使用 data_dir

每个插件有独立的数据目录：

```python
import json

async def handle(command: str, args: str, event: Dict, context) -> List:
    data_file = context.data_dir / "data.json"
    
    # 读取
    if data_file.exists():
        data = json.loads(data_file.read_text())
    else:
        data = {}
    
    # 修改
    data["count"] = data.get("count", 0) + 1
    
    # 保存
    data_file.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    
    return segments(f"已访问 {data['count']} 次")
```

### 使用 plugin_base 工具

```python
from core.plugin_base import load_json, write_json

async def handle(command: str, args: str, event: Dict, context) -> List:
    data_file = context.data_dir / "data.json"
    
    # 读取（文件不存在返回空字典）
    data = load_json(data_file)
    
    # 修改
    data["count"] = data.get("count", 0) + 1
    
    # 保存
    write_json(data_file, data)
    
    return segments(f"已访问 {data['count']} 次")
```

---

## 🔐 插件私有配置

### 在 secrets.json 中配置

```json
{
  "plugins": {
    "myplugin": {
      "api_key": "your-api-key",
      "endpoint": "https://api.example.com"
    }
  }
}
```

### 在插件中读取

`context.secrets` 保存 `secrets.json` 内容，插件配置在 `plugins.<plugin_name>` 路径下：

```python
async def handle(command: str, args: str, event: Dict, context) -> List:
    plugin_config = context.secrets.get("plugins", {}).get("myplugin", {})
    api_key = plugin_config.get("api_key")

    if not api_key:
        return segments("错误：未配置 API Key")

    # 使用配置
    ...
```

---

## 📝 日志记录

使用 `context.logger`：

```python
async def handle(command: str, args: str, event: Dict, context) -> List:
    context.logger.debug(f"收到命令: {command}, 参数: {args}")
    context.logger.info(f"处理用户 {event.get('user_id')} 的请求")
    context.logger.warning("这是一个警告")
    context.logger.error("发生错误", exc_info=True)  # 包含堆栈
    
    return segments("OK")
```

**日志级别**：
- `DEBUG` - 调试信息，生产环境通常关闭
- `INFO` - 一般信息
- `WARNING` - 警告
- `ERROR` - 错误

---

## 🛡️ 权限检查

### 管理员命令

在 `plugin.json` 中设置 `admin_only: true`：

```json
{
  "commands": [{
    "name": "admin_cmd",
    "triggers": ["admin"],
    "admin_only": true
  }]
}
```

框架会自动检查权限，非管理员调用会返回"权限不足"。

### 手动检查

```python
async def handle(command: str, args: str, event: Dict, context) -> List:
    user_id = event.get("user_id")
    admin_ids = context.secrets.get("admin_user_ids", [])
    
    if user_id not in admin_ids:
        return segments("你没有权限执行此操作")
    
    # 执行管理员操作
    ...
```

---

## 🛠️ 错误处理

### 基本模式

```python
async def handle(command: str, args: str, event: Dict, context) -> List:
    try:
        result = await do_something(args)
        return segments(f"成功: {result}")
    except ValueError as e:
        context.logger.warning(f"参数错误: {e}")
        return segments(f"参数错误: {e}")
    except Exception as e:
        context.logger.error(f"未知错误: {e}", exc_info=True)
        return segments("处理失败，请稍后重试")
```

### 优雅降级

```python
async def handle(command: str, args: str, event: Dict, context) -> List:
    # 尝试主要方案
    try:
        result = await primary_api()
        return segments(result)
    except Exception:
        context.logger.warning("主 API 失败，尝试备用")
    
    # 降级到备用方案
    try:
        result = await backup_api()
        return segments(result)
    except Exception:
        context.logger.error("备用 API 也失败")
        return segments("服务暂时不可用")
```

---

## 🌟 完整示例：天气插件

```python
"""
天气查询插件

使用: /天气 城市名
"""

from typing import Any, Dict, List
from core.plugin_base import segments

API_URL = "https://api.example.com/weather"


async def init(context):
    """初始化"""
    context.logger.info("天气插件已加载")


async def handle(
    command: str,
    args: str,
    event: Dict[str, Any],
    context
) -> List[Dict[str, Any]]:
    """处理天气查询"""
    city = args.strip()
    
    if not city:
        return segments("请输入城市名，如: /天气 北京")
    
    context.logger.info(f"查询城市天气: {city}")
    
    try:
        # 获取 API Key
        api_key = context.secrets.get("weather", {}).get("api_key")
        if not api_key:
            return segments("错误：未配置天气 API Key")
        
        # 请求天气 API
        async with context.http_session.get(
            API_URL,
            params={"city": city, "key": api_key}
        ) as resp:
            if resp.status != 200:
                return segments(f"查询失败: HTTP {resp.status}")
            
            data = await resp.json()
        
        # 格式化输出
        return segments(
            f"🌤 {city} 天气\n"
            f"━━━━━━━━━━\n"
            f"温度: {data['temp']}°C\n"
            f"湿度: {data['humidity']}%\n"
            f"天气: {data['weather']}\n"
            f"━━━━━━━━━━"
        )
        
    except Exception as e:
        context.logger.error(f"天气查询失败: {e}", exc_info=True)
        return segments("查询失败，请稍后重试")


async def shutdown(context):
    """清理"""
    context.logger.info("天气插件已卸载")
```

**plugin.json**：
```json
{
  "name": "weather",
  "version": "1.0.0",
  "description": "天气查询插件",
  "entry": "main.py",
  "commands": [{
    "name": "weather",
    "triggers": ["天气", "weather"],
    "help": "查询天气 | /天气 北京"
  }]
}
```

**secrets.json** 配置：
```json
{
  "plugins": {
    "weather": {
      "api_key": "your-weather-api-key"
    }
  }
}
```

---

## ➡️ 下一步

- 多轮对话开发见 [07-advanced.md](07-advanced.md#多轮对话)
- 定时任务开发见 [07-advanced.md](07-advanced.md#定时任务)
- API 参考见 [05-api-reference.md](05-api-reference.md)

---

### ⚡ 性能优化建议

1. **避免重复初始化**
   ```python
   # ❌ 不好：每次都初始化
   async def handle(command, args, event, context):
       client = create_client()
       ...
   
   # ✅ 好：在 init() 中初始化
   global client
   
   async def init(context):
       global client
       client = create_client()
   
   async def handle(command, args, event, context):
       use_client(client)
   ```

2. **使用缓存**
   ```python
   from functools import lru_cache
   
   @lru_cache(maxsize=100)
   def expensive_calculation(key: str) -> str:
       # 耗时操作
       ...
   
   async def handle(command, args, event, context):
       result = expensive_calculation(args)
       return segments(result)
   ```

3. **异步 I/O**
   ```python
   # ❌ 不好：阻塞主线程
   def handle_sync(...):
       time.sleep(5)  # 阻塞 5 秒
       ...
   
   # ✅ 好：使用异步
   async def handle_async(...):
       await asyncio.sleep(5)  # 不阻塞
       ...
   ```
