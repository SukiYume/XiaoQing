# 05 - API 参考

本章是完整的 API 参考手册。

---

## plugin_base 模块

导入方式：
```python
from core.plugin_base import (
    text, image, image_url, record, record_url,
    segments, build_action, run_sync,
    ensure_dir, load_json, write_json,
    PluginContext
)
```

### 消息段构建

#### text(content)
创建文本消息段。

```python
text("Hello World")
# 返回: {"type": "text", "data": {"text": "Hello World"}}
```

#### image(file_path)
创建本地图片消息段。

```python
image("/path/to/image.png")
# 返回: {"type": "image", "data": {"file": "file:///path/to/image.png"}}
```

#### image_url(url)
创建网络图片消息段。

```python
image_url("https://example.com/pic.jpg")
# 返回: {"type": "image", "data": {"file": "https://example.com/pic.jpg"}}
```

#### record(file_path)
创建本地语音消息段。

```python
record("/path/to/audio.mp3")
# 返回: {"type": "record", "data": {"file": "file:///path/to/audio.mp3"}}
```

#### record_url(url)
创建网络语音消息段。

```python
record_url("https://example.com/audio.mp3")
# 返回: {"type": "record", "data": {"file": "https://example.com/audio.mp3"}}
```

#### segments(payload)
将任意值转换为消息段列表。

```python
segments("Hello")      # -> [{"type": "text", "data": {"text": "Hello"}}]
segments(None)         # -> []
segments([text("Hi")]) # -> [{"type": "text", "data": {"text": "Hi"}}]
```

#### build_action(segs, user_id, group_id)
构建 OneBot Action。

```python
segs = [text("Hello")]
build_action(segs, user_id=123, group_id=None)
# 返回: {
#   "action": "send_private_msg",
#   "params": {"user_id": 123, "message": [...]}
# }

build_action(segs, user_id=123, group_id=456)
# 返回: {
#   "action": "send_group_msg",
#   "params": {"group_id": 456, "message": [...]}
# }
```

### 异步工具

#### run_sync(func, *args, **kwargs)
在线程池中运行同步函数。

```python
import requests

async def handle(...):
    # 避免阻塞事件循环
    response = await run_sync(requests.get, "https://api.example.com")
    return segments(response.text)
```

### 文件工具

#### ensure_dir(path)
确保目录存在（递归创建）。

```python
ensure_dir(Path("/path/to/dir"))
```

#### load_json(path, default=None)
加载 JSON 文件。

```python
data = load_json(Path("data.json"))  # 文件不存在返回 {}
data = load_json(Path("data.json"), default={"count": 0})
```

#### write_json(path, data)
写入 JSON 文件。

```python
write_json(Path("data.json"), {"count": 1})
```

---

## PluginContext 类

插件处理函数的 `context` 参数类型。

### 属性

| 属性 | 类型 | 说明 |
|------|------|------|
| `config` | `Dict[str, Any]` | config.json 内容 |
| `secrets` | `Dict[str, Any]` | secrets.json 内容 |
| `plugin_name` | `str` | 当前插件名 |
| `plugin_dir` | `Path` | 插件目录路径 |
| `data_dir` | `Path` | 数据目录路径 |
| `logger` | `logging.Logger` | 日志记录器 |
| `http_session` | `aiohttp.ClientSession` | HTTP 客户端 |
| `current_user_id` | `Optional[int]` | 当前消息的用户 ID |
| `current_group_id` | `Optional[int]` | 当前消息的群 ID |

### 方法

#### default_groups()
获取配置的默认群列表。

```python
groups = context.default_groups()  # -> [123456, 789012]
```

#### reload_config()
重新加载配置文件。

```python
context.reload_config()
```

#### reload_plugins()
重新加载所有插件。

```python
context.reload_plugins()
```

#### list_commands()
获取所有已注册命令。

```python
commands = context.list_commands()  # -> ["help", "echo", ...]
```

#### list_plugins()
获取所有已加载插件。

```python
plugins = context.list_plugins()  # -> ["core", "echo", ...]
```

### 会话方法

#### create_session(initial_data=None, timeout=300.0)
为当前用户创建会话。

```python
session = await context.create_session(
    initial_data={"step": 1, "target": 50},
    timeout=180.0  # 3 分钟超时
)
```

**参数**：
- `initial_data` - 初始会话数据
- `timeout` - 超时时间（秒）

**返回**：`Session` 对象

#### get_session()
获取当前用户的会话。

```python
session = await context.get_session()
if session:
    step = session.get("step")
```

**返回**：`Session` 或 `None`（无会话或已过期）

#### end_session()
结束当前用户的会话。

```python
await context.end_session()
```

**返回**：`bool` - 是否成功删除

#### has_session()
检查当前用户是否有活跃会话。

```python
if await context.has_session():
    ...
```

### 静音方法

#### mute_group(group_id, duration_minutes)
静音指定群。

```python
context.mute_group(123456, 30)  # 静音 30 分钟
```

#### unmute_group(group_id)
解除群静音。

```python
context.unmute_group(123456)
```

#### is_group_muted(group_id)
检查群是否被静音。

```python
if context.is_group_muted(123456):
    ...
```

#### get_mute_remaining(group_id)
获取剩余静音时间（秒）。

```python
remaining = context.get_mute_remaining(123456)  # -> 930.5 (秒)
```

**返回值**：`float` - 剩余静音时间（秒），0 表示未静音

**注意**：新增方法

---

## Dispatcher 类

Dispatcher 提供了静音控制的便捷方法，插件可以通过 `context.dispatcher` 访问。

### 属性

| 属性 | 类型 | 说明 |
|------|------|------|
| `router` | `CommandRouter` | 命令路由器 |
| `app` | `XiaoQingApp` | 应用实例 |
| `session_manager` | `SessionManager` | 会话管理器 |
| `_handlers` | `tuple[MessageHandler, ...]` | Handler 链 |

### 方法

#### is_admin(user_id: Optional[int]) -> bool
判断用户是否是管理员。

```python
if context.dispatcher.is_admin(user_id):
    return segments("管理员命令")
else:
    return segments("权限不足")
```

#### mute_group(group_id: int, duration_minutes: float) -> None
静音指定群。

```python
context.dispatcher.mute_group(123456, 30)  # 静音 30 分钟
```

#### unmute_group(group_id: int) -> None
解除群静音。

```python
context.dispatcher.unmute_group(123456)
```

#### is_muted(group_id: Optional[int]) -> bool
检查群是否被静音。

```python
if context.dispatcher.is_muted(123456):
    return segments("群聊已静音")
```

#### get_mute_remaining(group_id: int) -> Optional[float]
获取剩余静音时间（秒）。

```python
remaining = context.dispatcher.get_mute_remaining(123456)
if remaining:
    return segments(f"剩余静音时间：{remaining/60:.1f} 分钟")
```

---

## Session 类增强

### 新增方法

#### get_remaining_time() -> float
获取会话剩余时间（秒）。

```python
remaining = session.get_remaining_time()
if remaining < 60:
    return segments(f"会话将在 {remaining} 秒后过期")
```

**返回值**：`float` - 剩余时间（秒）

#### is_active() -> bool
检查会话是否活跃。

```python
if session.is_active():
    return segments("会话进行中")
```

**返回值**：`bool` - 会话是否活跃（未过期）

---

## ProcessDecision 类

处理决策数据类，用于决策判断。

### 定义

```python
@dataclass
class ProcessDecision:
    """处理决策"""
    should_process: bool    # 是否应该处理
    smalltalk_mode: bool    # 是否进入闲聊模式
```

### 使用场景

此数据类由 `Dispatcher._make_decision()` 返回，决定消息如何处理。

**示例**：

```python
# xiaoqing_chat 特殊处理
if smalltalk_provider == "xiaoqing_chat":
    # 所有群聊消息都进入 SmalltalkHandler
    return ProcessDecision(True, True)

# 私聊始终处理
if is_private:
    return ProcessDecision(True, True)

# 群聊有命令前缀
if has_prefix:
    return ProcessDecision(True, False)  # 不进入闲聊模式

# 群聊随机触发
if random.random() < random_reply_rate:
    return ProcessDecision(True, True)  # 进入闲聊模式

# 其他情况不处理
return ProcessDecision(False, False)
```

### 决策影响

| `should_process` | `smalltalk_mode` | 结果 |
|-----------------|-----------------|------|
| `False` | 任意 | 不处理，直接返回 `[]` |
| `True` | `False` | 处理，但不进入 SmalltalkHandler |
| `True` | `True` | 处理，可进入 SmalltalkHandler |

---

## Handler 链

框架引入了责任链模式，Handler 链按优先级依次处理消息。

### Handler 类型

| Handler | 优先级 | 职责 | 调用函数 |
|---------|---------|------|---------|
| `BotNameHandler` | 1 | 处理仅机器人名字 | - |
| `CommandHandler` | 2 | 命令匹配和执行 | `handle()` |
| `SessionHandler` | 3 | 活跃会话处理 | `handle_session()` |
| `SmalltalkHandler` | 4 | 闲聊处理 | `handle_smalltalk()` |

### 执行流程

```python
# Dispatcher 内部执行
for handler in self._handlers:
    result = await handler.handle(text, event, context)
    
    if result is not None:
        # 短路：返回结果，不再执行后续 Handler
        return result

# 所有 Handler 都不处理
return []
```

### 短路机制

- **短路**：一旦某个 Handler 返回非 `None` 结果，后续 Handler 不会执行
- **优先级**：命令优先于会话，会话优先于闲聊
- **独立性**：每个 Handler 独立判断是否处理

### 示例场景

**场景 1：用户发送命令 `/help`**

```
1. BotNameHandler: 不是仅机器人名字 → None
2. CommandHandler: 匹配成功！
   → 调用 help.handle()
   → 返回帮助信息
   → 短路 ✅
3. SessionHandler: 不执行 ❌
4. SmalltalkHandler: 不执行 ❌
```

**场景 2：用户在会话中发送消息**

```
1. BotNameHandler: None
2. CommandHandler: 无匹配命令 → None
3. SessionHandler: 发现活跃会话！
   → 调用 guess.handle_session()
   → 返回 ["太大了！"]
   → 短路 ✅
4. SmalltalkHandler: 不执行 ❌
```

**场景 3：用户发送闲聊消息**

```
1. BotNameHandler: None
2. CommandHandler: 无匹配命令 → None
3. SessionHandler: 无活跃会话 → None
4. SmalltalkHandler: smalltalk_mode=True
   → 调用 xiaoqing_chat.handle_smalltalk()
   → 返回 ["你好！"]
   → 短路 ✅
```

---

## Session 类

多轮对话的会话对象。

### 属性

| 属性 | 类型 | 说明 |
|------|------|------|
| `user_id` | `int` | 用户 ID |
| `group_id` | `Optional[int]` | 群 ID（私聊为 None） |
| `plugin_name` | `str` | 所属插件 |
| `state` | `str` | 会话状态 |
| `data` | `Dict` | 会话数据 |
| `timeout` | `float` | 超时时间（秒） |

### 方法

#### get(key, default=None)
获取会话数据。

```python
step = session.get("step", 1)
```

#### set(key, value)
设置会话数据（自动更新时间戳）。

```python
session.set("step", 2)
session.set("attempts", session.get("attempts", 0) + 1)
```

#### clear()
清空会话数据。

```python
session.clear()
```

#### is_expired()
检查是否过期。

```python
if session.is_expired():
    ...
```

---

## handle() 函数签名

插件入口函数。

```python
async def handle(
    command: str,           # 命令名
    args: str,              # 参数字符串
    event: Dict[str, Any],  # 原始 OneBot 事件
    context: PluginContext  # 插件上下文
) -> List[Dict[str, Any]]:  # 返回消息段列表
    ...
```

### event 参数常用字段

```python
event = {
    "post_type": "message",
    "message_type": "group",  # 或 "private"
    "user_id": 123456,
    "group_id": 789012,       # 私聊时为 None
    "message": [              # 消息段列表
        {"type": "text", "data": {"text": "内容"}}
    ],
    "raw_message": "内容",    # 原始消息文本
    "sender": {
        "user_id": 123456,
        "nickname": "昵称",
        "card": "群名片",
        "role": "member"      # member/admin/owner
    },
    "time": 1234567890
}
```

---

## handle_session() 函数签名

多轮对话处理函数（可选）。

### 概述

当用户处于活跃会话时，此函数会被调用处理用户的后续消息。在 Handler 链式架构中，`SessionHandler` 负责调用此函数。

### 函数签名

```python
async def handle_session(
    text: str,              # 用户输入的文本
    event: Dict[str, Any],  # 原始 OneBot 事件
    context: PluginContext, # 插件上下文
    session: Session        # 会话对象
) -> List[Dict[str, Any]]:  # 返回消息段列表
    ...
```

### 参数说明

| 参数 | 类型 | 说明 |
|------|------|------|
| `text` | `str` | 用户输入的文本（未经过任何处理） |
| `event` | `Dict[str, Any]` | 原始 OneBot 事件 |
| `context` | `PluginContext` | 插件上下文 |
| `session` | `Session` | 当前会话对象 |

### 返回值

返回消息段列表，表示要发送的回复。

### 会话生命周期

```
用户发送命令（如 /guess）
    ↓
插件调用 context.create_session()
    ↓
会话创建，状态为 active
    ↓
用户后续消息
    ↓
SessionHandler 捕获
    ↓
调用 handle_session()
    ↓
插件处理并返回回复
    ↓
会话更新（session.set()）
    ↓
┌─────────────┬─────────────┐
│ 继续对话      │ 结束对话      │
│ (返回消息段)  │ 调用 end_session())
└─────────────┴─────────────┘
    ↓              ↓
回到 handle_session()   会话被删除
```

### 使用示例

```python
async def handle(command: str, args: str, event: Dict, context) -> List:
    """开始猜数字游戏"""
    target = random.randint(1, 100)
    
    # 创建会话
    await context.create_session(
        initial_data={
            "target": target,
            "attempts": 0,
            "start_time": time.time()
        },
        timeout=180  # 3 分钟超时
    )
    
    return segments(
        "🎮 猜数字游戏开始！\n"
        "我已经想好了一个 1-100 的数字\n"
        "请输入你的猜测"
    )


async def handle_session(text: str, event: Dict, context, session) -> List:
    """处理游戏中的消息"""
    
    # 退出命令
    if text.lower() in ["退出", "quit", "q"]:
        target = session.get("target")
        await context.end_session()
        return segments(f"游戏结束，答案是 {target}")
    
    # 解析猜测
    try:
        guess = int(text.strip())
    except ValueError:
        return segments("请输入有效的数字")
    
    # 更新尝试次数
    attempts = session.get("attempts", 0) + 1
    session.set("attempts", attempts)
    
    # 获取目标数字
    target = session.get("target")
    
    # 判断结果
    if guess < target:
        return segments(f"太小了！（{attempts} 次尝试）")
    elif guess > target:
        return segments(f"太大了！（{attempts} 次尝试）")
    else:
        # 猜对了，结束会话
        elapsed = int(time.time() - session.get("start_time"))
        await context.end_session()
        return segments(
            f"🎉 恭喜你猜对了！\n"
            f"答案：{target}\n"
            f"尝试次数：{attempts}\n"
            f"用时：{elapsed} 秒"
        )
```

### 架构特性

在 Handler 链式架构中：

1. **优先级明确**：会话处理在命令匹配之后、闲聊之前
2. **绕过触发条件**：即使 `should_process = False`，活跃会话仍会处理
3. **独立处理**：会话处理不受 `random_reply_rate` 或 `bot_name` 影响

### 注意事项

1. **会话超时**
   - 超过 `timeout` 时间后，会话自动过期
   - 用户下次发送消息时会创建新会话

2. **每个用户独立**
   - 每个 `(user_id, group_id)` 组合有独立的会话
   - 私聊和群聊的会话互不影响

3. **手动结束**
   - 游戏或对话结束时，必须调用 `context.end_session()`
   - 否则用户需要等待超时才能开始新会话

4. **数据更新**
   - 使用 `session.set()` 更新数据会自动刷新 `updated_at` 时间戳
   - 这会延长会话的有效期

---

## handle_url() 函数签名

URL 自动解析函数（可选）。

```python
async def handle_url(
    url: str,               # 提取到的 URL
    event: Dict[str, Any],  # 原始 OneBot 事件
    context: PluginContext  # 插件上下文
) -> List[Dict[str, Any]]:  # 返回消息段列表
    ...
```

---

## handle_smalltalk() 函数签名
闲聊处理函数（可选）。

### 概述

当插件被配置为 `smalltalk_provider` 时，此函数会被调用处理闲聊消息。

### 函数签名

```python
async def handle_smalltalk(
    text: str,              # 用户消息文本（已去除前缀）
    event: Dict[str, Any],  # 原始 OneBot 事件
    context: PluginContext  # 插件上下文
) -> List[Dict[str, Any]]:  # 返回消息段列表，或 None
    ...
```

### 返回值

| 返回值 | 说明 |
|--------|------|
| `List[Dict]` | 返回消息段列表，表示需要回复 |
| `None` 或 `[]` | 不回复，传递给后续处理或直接返回空 |

### 使用示例

```python
async def handle_smalltalk(text: str, event: Dict, context) -> List:
    """简单规则闲聊"""
    
    # 1. 检查是否应该回复
    if not should_reply(text, event):
        return None  # 不回复
    
    # 2. 生成回复
    if "你好" in text or "hello" in text.lower():
        return segments("你好！有什么我可以帮助你的吗？")
    
    if "名字" in text or "你是谁" in text:
        bot_name = context.config.get("bot_name", "小青")
        return segments(f"我叫 {bot_name}~")
    
    # 3. 不回复其他消息
    return None
```

### xiaoqing_chat 特殊处理

当 `smalltalk_provider` 配置为 `xiaoqing_chat` 时：

1. **所有群聊消息都会调用此函数**
   - 不受 `random_reply_rate` 配置影响
   - 由插件内部控制回复频率

2. **插件可以自主决定是否回复**
   - 返回 `None` 或 `[]` 表示不回复
   - 返回消息段列表表示需要回复

3. **可以实现更复杂的逻辑**
   ```python
   async def handle_smalltalk(text: str, event: Dict, context) -> List:
       # 1. 获取用户历史
       user_id = event.get("user_id")
       history = await get_user_history(user_id, context)
       
       # 2. 情绪分析
       sentiment = analyze_sentiment(text)
       
       # 3. 根据情绪和历史决定是否回复
       if sentiment < 0 and history["negative_count"] > 3:
           return None  # 用户情绪不好，暂不回复
       
       # 4. 生成回复
       response = await generate_llm_response(text, history, context)
       
       # 5. 保存到历史
       await save_to_history(user_id, text, response)
       
       return segments(response)
   ```

### 配置为 smalltalk_provider

在 `config.json` 中配置：

```json
{
  "plugins": {
    "smalltalk_provider": "your_plugin_name"
  }
}
```

在 `secrets.json` 中配置插件私有配置：

```json
{
  "plugins": {
    "your_plugin_name": {
      "api_key": "your-api-key",
      "base_url": "https://api.example.com"
    }
  }
}
```

---

## init() / shutdown() 钩子

```python
async def init(context: PluginContext) -> None:
    """插件加载时调用"""
    ...

async def shutdown(context: PluginContext) -> None:
    """插件卸载时调用"""
    ...
```

---

## 定时任务处理函数

```python
async def handler_name(context: PluginContext) -> List[Dict[str, Any]]:
    """定时任务处理函数"""
    return segments("定时消息")
```

---

## Inbound Server API

### POST /event

接收 OneBot 事件推送。

**请求头**：
```
Authorization: Bearer <inbound_token>
Content-Type: application/json
```

**请求体**（OneBot 事件）：
```json
{
  "post_type": "message",
  "message_type": "group",
  "group_id": 123456,
  "user_id": 789,
  "message": [{"type": "text", "data": {"text": "/help"}}]
}
```

**响应体**：
```json
{
  "actions": [
    {
      "action": "send_group_msg",
      "params": {
        "group_id": 123456,
        "message": [{"type": "text", "data": {"text": "帮助信息"}}]
      }
    }
  ]
}
```

### WebSocket /ws

WebSocket 端点，用于持久连接。

**连接**：
```
ws://127.0.0.1:12000/ws
Header: Authorization: Bearer <token>
```

**消息格式**：同 POST /event

### GET /health

健康检查。

**响应**：
```json
{"status": "ok"}
```

---

## OneBot Action 格式

XiaoQing 返回的 Action 遵循 OneBot 协议。

### send_group_msg

发送群消息。

```json
{
  "action": "send_group_msg",
  "params": {
    "group_id": 123456,
    "message": [{"type": "text", "data": {"text": "内容"}}]
  }
}
```

### send_private_msg

发送私聊消息。

```json
{
  "action": "send_private_msg",
  "params": {
    "user_id": 789,
    "message": [{"type": "text", "data": {"text": "内容"}}]
  }
}
```

---

## 下一步

- 配置详解 → [06-configuration.md](06-configuration.md)
- 高级主题 → [07-advanced.md](07-advanced.md)
