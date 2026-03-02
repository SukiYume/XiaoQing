# 03 - 插件开发指南

本章是插件开发的完整指南，从最简单的插件到高级功能。

---

## 插件基础

### 插件结构

每个插件应该是一个Python包（包含 `__init__.py`），位于 `plugins/` 目录下：

```
plugins/
└── myplugin/
    ├── plugin.json     # 必需：插件配置
    ├── main.py         # 必需：入口代码
    ├── __init__.py     # 推荐：使插件成为 Python 包
    ├── config.py       # 可选：配置文件
    ├── utils.py        # 可选：工具函数
    └── data/           # 可选：数据目录（自动创建）
```

### 导入规范

从 v2.0 开始，插件被加载为标准的 Python 包 (`xiaoqing_plugins.plugin_name`)。这意味着你可以（并且应该）使用**相对导入**来引用插件内的其他模块：

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

## plugin.json 配置

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

## main.py 编写

### Handler 链式处理机制

框架引入了 Handler 链式处理模式，所有插件命令都通过 **CommandHandler** 执行。了解 Handler 链有助于开发更复杂的插件。

#### Handler 链处理流程

```
消息到达 Dispatcher
    ↓
决策判断 (should_process)
    ↓
Handler 链依次尝试：
    ↓
┌─────────────────────────────┐
│ 1. BotNameHandler         │ ← 处理仅机器人名字（如"小青"）
│    - 返回固定回复或帮助   │
└──────────┬────────────────┘
           │ 失败（返回 None）
           ▼
┌─────────────────────────────┐
│ 2. CommandHandler        │ ← 你的插件命令在这里执行
│    - 匹配触发词          │
│    - 调用 handle()       │
│    - 检查权限           │
└──────────┬────────────────┘
           │ 失败（无匹配命令）
           ▼
┌─────────────────────────────┐
│ 3. SessionHandler        │ ← 活跃会话处理
│    - 调用 handle_session()│
└──────────┬────────────────┘
           │ 失败（无活跃会话）
           ▼
┌─────────────────────────────┐
│ 4. SmalltalkHandler      │ ← 闲聊处理
│    - 调用 handle_smalltalk()│
└──────────┬────────────────┘
           │ 失败（不回复）
           ▼
       返回 []
```

#### 插件与 Handler 链的交互

1. **命令处理**（CommandHandler）
   - 用户发送 `/your_command args`
   - CommandHandler 匹配成功
   - 调用你的 `handle()` 函数
   - **短路机制**：一旦返回非空结果，后续 Handler 不会执行

2. **会话处理**（SessionHandler）
   - 用户在会话中发送消息
   - SessionHandler 发现活跃会话
   - 调用你的 `handle_session()` 函数
   - **会话优先级**：会话处理在命令匹配之后

3. **闲聊处理**（SmalltalkHandler）
   - 如果你的插件是 `smalltalk_provider`
   - SmalltalkHandler 调用你的 `handle_smalltalk()` 函数
   - **智能回复**：你可以根据上下文决定是否返回消息

#### 短路机制示例

```python
# 场景：用户在猜数字会话中，同时发送了命令
# 用户的会话状态：guess_game = True

# Handler 链执行：
# 1. BotNameHandler: None
# 2. CommandHandler: 匹配到 /guess 命令
#    → 调用 guess.handle()
#    → 返回 ["游戏开始！"]
#    → 短路，后续 Handler 不执行 ❌
#    → SessionHandler 不会处理

# 场景：用户在会话中，但没有发送命令
# 用户的会话状态：guess_game = True

# Handler 链执行：
# 1. BotNameHandler: None
# 2. CommandHandler: 无匹配命令 → None
# 3. SessionHandler: 发现活跃会话！
#    → 调用 guess.handle_session()
#    → 返回 ["太大了！"]
#    → 短路，后续 Handler 不执行 ❌
#    → SmalltalkHandler 不会处理
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

如果你的插件是 `smalltalk_provider`（如 xiaoqing_chat），需要实现此函数。

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

**重要特性**：

1. **智能回复控制**
   - 不同于简单的 `random_reply_rate`，你可以根据上下文判断
   - 返回 `[]` 表示不回复
   - 返回非空列表表示回复

2. **xiaoqing_chat 特殊处理**
   - 当 `smalltalk_provider` 设置为 `xiaoqing_chat` 时
   - 所有群聊消息都会进入 `handle_smalltalk()`
   - `random_reply_rate` 配置失效
   - 由插件内部控制回复频率

3. **与其他 Handler 的关系**
   - SmalltalkHandler 是 Handler 链的最后一环
   - 只有在前面所有 Handler 都失败时才会执行
   - 如果用户在会话中，SmalltalkHandler 不会执行

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

## PluginContext 详解

`context` 是插件的上下文对象，提供各种工具。

### 属性

```python
# 配置
context.config       # Dict - config.json 内容
context.secrets      # Dict - secrets.json 内容

# 路径
context.plugin_name  # str - 插件名
context.plugin_dir   # Path - 插件目录 (plugins/myplugin/)
context.data_dir     # Path - 数据目录 (plugins/myplugin/data/)

# 工具
context.logger       # Logger - 日志记录器
context.http_session # aiohttp.ClientSession - HTTP 客户端

# 当前消息上下文
context.current_user_id   # int | None
context.current_group_id  # int | None
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

会话是 Handler 链的第三环（SessionHandler），用于实现多轮对话。

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
4. 用户后续消息被 SessionHandler 捕获
       │
       ▼
5. 调用 handle_session() 而非 handle()
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

## 消息构建

### 基础函数

```python
from core.plugin_base import text, image, image_url, record, record_url, segments

# 文本
text("Hello")
# -> {"type": "text", "data": {"text": "Hello"}}

# 图片（本地文件）
image("/path/to/image.png")
# -> {"type": "image", "data": {"file": "file:///path/to/image.png"}}

# 图片（URL）
image_url("https://example.com/pic.jpg")
# -> {"type": "image", "data": {"file": "https://example.com/pic.jpg"}}

# 语音（本地文件）
record("/path/to/audio.mp3")

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

## 生命周期钩子

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

插件卸载时调用，用于清理资源。**注意：此钩子有 5 秒的超时限制，超时将被强制中断。**

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

## HTTP 请求

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

## 数据持久化

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

## 插件私有配置

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

```python
async def handle(command: str, args: str, event: Dict, context) -> List:
    # 方式一：直接访问
    plugin_config = context.secrets.get("plugins", {}).get("myplugin", {})
    api_key = plugin_config.get("api_key")
    
    # 方式二：使用 context.secrets（已自动提取 plugins 部分）
    api_key = context.secrets.get("myplugin", {}).get("api_key")
    
    if not api_key:
        return segments("错误：未配置 API Key")
    
    # 使用配置
    ...
```

---

## 日志记录

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

## 权限检查

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

## 错误处理

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

## 完整示例：天气插件

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

## 下一步

- 多轮对话开发 → [07-advanced.md](07-advanced.md#多轮对话)
- 定时任务开发 → [07-advanced.md](07-advanced.md#定时任务)
- API 完整参考 → [05-api-reference.md](05-api-reference.md)

---

### 性能优化建议

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
