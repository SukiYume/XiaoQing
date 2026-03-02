# 消息处理流程

本文档详细描述 XiaoQing 的消息处理逻辑，包括消息接收、路由、命令解析、会话管理等完整流程。

---

## 目录

1. [消息处理总览](#1-消息处理总览)
2. [消息接收与解析](#2-消息接收与解析)
3. [触发条件判断](#3-触发条件判断)
4. [命令路由与参数拆分](#4-命令路由与参数拆分)
5. [会话管理](#5-会话管理)
6. [闲聊处理](#6-闲聊处理)
7. [静音机制](#7-静音机制)
8. [并发控制与消息队列](#8-并发控制与消息队列)
9. [完整处理流程图](#9-完整处理流程图)

---

## 1. 消息处理总览

XiaoQing 的消息处理由以下核心模块协作完成：

| 模块 | 文件 | 职责 |
|------|------|------|
| **Dispatcher** | `core/dispatcher.py` | 消息分发器，协调整个处理流程 |
| **CommandRouter** | `core/router.py` | 命令路由，匹配触发词并拆分参数 |
| **SessionManager** | `core/session.py` | 会话管理，支持多轮对话 |
| **message** | `core/message.py` | 消息解析工具函数 |

### 1.1 处理流程概述

```
OneBot 事件
    ↓
Dispatcher.handle_event()
    ↓
┌─────────────────────────────────────────────────────────────┐
│  1. 事件类型检查（仅处理 message 类型）                      │
│  2. 消息解析（提取文本、user_id、group_id）                  │
│  3. URL 检测（全局监听，可选）                               │
│  4. 决策判断（是否需要处理）                                 │
│  5. Handler 链式处理（按优先级依次尝试）                     │
│     - BotNameHandler  → 仅机器人名字                        │
│     - CommandHandler  → 命令匹配                            │
│     - SessionHandler  → 活跃会话                            │
│     - SmalltalkHandler → 闲聊                               │
└─────────────────────────────────────────────────────────────┘
    ↓
返回 OneBot 消息段列表
```

---

### 1.2 Handler 链式架构

XiaoQing 采用 **责任链模式** 来处理消息，每个 Handler 按顺序尝试处理消息，如果处理成功则返回，否则传递给下一个 Handler。

#### Handler 链顺序

```python
self._handlers: tuple[MessageHandler, ...] = (
    BotNameHandler(self),      # 1. 处理仅提及机器人名字的消息
    CommandHandler(self),       # 2. 匹配并执行命令
    SessionHandler(self),       # 3. 处理活跃会话
    SmalltalkHandler(self),    # 4. 处理闲聊
)
```

#### 各 Handler 职责

| Handler | 处理场景 | 返回条件 |
|---------|---------|---------|
| **BotNameHandler** | 用户仅发送机器人名字（如 "小青"） | 文本仅包含 bot_name 或其变体 |
| **CommandHandler** | 用户发送命令（如 "/help"） | 命令路由匹配成功 |
| **SessionHandler** | 用户处于活跃会话中 | 存在活跃会话 |
| **SmalltalkHandler** | 其他情况（闲聊） | smalltalk_mode 为 True |

#### 关键特性

1. **短路机制**：一旦某个 Handler 处理成功，后续 Handler 不会执行
2. **优先级明确**：命令优先于会话，会话优先于闲聊
3. **独立决策**：每个 Handler 独立决定是否处理，互不影响

#### xiaoqing_chat 特殊处理

当 `smalltalk_provider` 设置为 `xiaoqing_chat` 时，决策逻辑特殊：

```python
if self._get_smalltalk_provider() == "xiaoqing_chat":
    return ProcessDecision(True, True)  # 所有消息都允许进入 SmalltalkHandler
```

这意味着：
- **`random_reply_rate` 配置失效** - 所有群聊消息都会进入 xiaoqing_chat
- **插件自主控制** - xiaoqing_chat 插件内部有自己的频率控制和回复概率判断
- **返回空列表不回复** - 如果插件决定不回复，返回 `[]` 即可

这种设计让 LLM 模型能够根据上下文智能判断是否需要回复，比简单的随机概率更智能。

---

## 2. 消息接收与解析

### 2.1 事件格式

XiaoQing 接收 OneBot 标准格式的消息事件：

```json
{
    "post_type": "message",
    "message_type": "group",
    "user_id": 123456789,
    "group_id": 987654321,
    "message": [
        {"type": "text", "data": {"text": "/help 查看帮助"}}
    ]
}
```

### 2.2 消息解析

`normalize_message()` 函数从事件中提取关键信息：

```python
text, user_id, group_id = normalize_message(event)
# text: "/help 查看帮助"
# user_id: 123456789
# group_id: 987654321 (私聊时为 None)
```

### 2.3 文本提取

`extract_text()` 函数从 OneBot 消息段中提取纯文本：

- **字符串消息**: 直接返回
- **消息段数组**: 提取所有 `type: "text"` 段的文本并拼接
- 其他类型（图片、@等）: 被忽略

```python
# 输入
message = [
    {"type": "at", "data": {"qq": "123"}},
    {"type": "text", "data": {"text": "你好"}},
    {"type": "image", "data": {"file": "abc.jpg"}},
    {"type": "text", "data": {"text": "世界"}}
]

# 输出
text = "你好世界"
```

---

## 3. 决策判断

### 3.1 决策逻辑

`_make_decision()` 方法判断消息是否需要处理，返回 `ProcessDecision(should_process, smalltalk_mode)`：

| 场景 | should_process | smalltalk_mode | 说明 |
|------|----------------|----------------|------|
| **私聊** | ✅ True | ✅ True | 私聊消息始终处理，可闲聊 |
| **群聊 + 命令前缀** | ✅ True | ❌ False | 如 `/help`，不触发闲聊 |
| **群聊 + 包含 bot_name** | ✅ True | ⚠️ 取决于静音 | 如 `小青 你好` |
| **群聊 + 随机触发** | ✅ True | ✅ True | 按 `random_reply_rate` 概率 |
| **群聊 + 静音中** | ❌ False | ❌ False | 除非有命令前缀或 bot_name |

### 3.2 决策与 Handler 链的关系

**重要理解**：决策判断的结果会影响 Handler 链的执行：

1. **`should_process = False`**：直接返回 `[]`，所有 Handler 都不会执行
2. **`should_process = True`**：进入 Handler 链，按顺序尝试各个 Handler

**特殊场景**：
- **活跃会话存在**：无论 `should_process` 如何，`SessionHandler` 都会处理（会话优先级最高）
- **xiaoqing_chat 作为 smalltalk_provider**：决策逻辑特殊，所有群聊消息都返回 `(True, True)`，`random_reply_rate` 失效

### 3.3 配置项

```json
{
    "bot_name": "小青",
    "command_prefixes": ["/"],
    "require_bot_name_in_group": true,
    "random_reply_rate": 0.05
}
```

- **bot_name**: 机器人名称，群聊中提及时触发
- **command_prefixes**: 命令前缀列表，通常为 `["/"]`
- **require_bot_name_in_group**: 群聊是否需要 @ 或提及 bot_name
- **random_reply_rate**: 无触发条件时随机回复的概率 (0-1)

### 3.4 前缀剥离

`_strip_prefix()` 方法按照以下顺序严格处理：

1. **去除 @机器人**（例如 `[CQ:at,qq=123] `）
2. **去除 bot_name**（例如 `小青`，支持模糊匹配及其后的标点）
3. **去除 command_prefixes**（例如 `/`）

**⚠️ 重要解析规则：**

当用户输入 **`小青配置`** 时：
1. `bot_name`（小青）首先被检测并移除，剩余文本变为 **`配置`**。
2. 随后尝试移除命令前缀（如 `/`），因不匹配而跳过。
3. 最终传递给 Router 的文本是 **`配置`**。

这意味着：如果你的插件命令触发词定义为 `["小青配置"]`，将会**匹配失败**，因为 Router 看到的是 `"配置"`。

**最佳实践**：
建议在 `plugin.json` 中定义触发词时，包含剥离 bot 名后的版本。

```python
# 输入: "小青 /help 查看帮助"
# 1. 剥离 bot_name -> "/help 查看帮助"
# 2. 剥离 prefix   -> "help 查看帮助"
# 结果: 匹配 trigger "help"
```

```python
# 输入: "小青配置"
# 1. 剥离 bot_name -> "配置"
# 2. 剥离 prefix   -> "配置" (无前缀可剥离)
# 结果: 需匹配 trigger "配置" (因此建议在 json 中添加 "配置" 作为 trigger)
```

---

## 4. 命令路由与参数拆分

### 4.1 命令注册

每个插件在 `plugin.json` 中声明命令：

```json
{
    "commands": [
        {
            "name": "help",
            "triggers": ["help", "h", "帮助"],
            "help": "查看帮助 | /help [关键词]",
            "admin_only": false,
            "priority": 0
        }
    ]
}
```

### 4.2 路由匹配

`CommandRouter.resolve()` 方法匹配命令：

```python
resolved = router.resolve("help 查看帮助")
# resolved = (CommandSpec, args)
# spec.name = "help"
# spec.plugin = "core"
# args = "查看帮助"
```

**匹配规则**：
1. 遍历所有注册的命令
2. 检查文本是否以任意 trigger 开头
3. 按优先级排序（priority 越大越优先）
4. 同优先级时，trigger 越长越优先

### 4.3 参数拆分

匹配成功后，trigger 后面的文本作为 `args` 传递给 handler：

```
输入文本: "echo 你好 世界"
匹配 trigger: "echo"
args: "你好 世界"
```

插件可使用 `core.args` 模块进一步解析参数：

```python
from core.args import parse

parsed = parse("你好 世界 -v --name=test")
# parsed.tokens = ["你好", "世界"]
# parsed.first = "你好"
# parsed.second = "世界"
# parsed.opt("v") = "true"
# parsed.opt("name") = "test"
```

### 4.4 Handler 调用

```python
async def handle(command: str, args: str, event: dict, context) -> List[dict]:
    """
    Args:
        command: 命令名（plugin.json 中的 name）
        args: 参数字符串（trigger 后的部分）
        event: 原始 OneBot 事件
        context: 插件上下文
    
    Returns:
        OneBot 消息段列表
    """
```

---

## 5. 会话管理

### 5.1 会话触发

**会话是 Handler 链的第三环**，在命令匹配失败后执行：

```python
# SessionHandler 处理逻辑
session = await session_manager.get(user_id, group_id)
if session:
    # 路由到会话插件的 handle_session()
```

**重要特性**：
- **优先级**：会话处理在命令之后、闲聊之前
- **绕过触发条件**：即使 `should_process = False`，活跃会话仍会处理
- **独立处理**：会话处理不受 `random_reply_rate` 或 `bot_name` 影响

### 5.2 会话创建

插件通过 `context.create_session()` 创建会话：

```python
async def handle(command, args, event, context):
    session = await context.create_session(
        initial_data={"target": 42},
        timeout=180  # 3 分钟超时
    )
    return segments("游戏开始！")
```

### 5.3 会话处理

当用户在会话中发送消息时，调用 `handle_session()`：

```python
async def handle_session(text: str, event: dict, context, session) -> List[dict]:
    """
    Args:
        text: 用户输入的文本
        event: 原始事件
        context: 插件上下文
        session: 当前会话对象
    """
    guess = int(text)
    target = session.get("target")
    if guess == target:
        await context.end_session()
        return segments("恭喜，猜对了！")
```

### 5.4 退出命令

以下命令可退出会话：
- `退出`、`取消`、`exit`、`quit`、`q`

---

## 6. 闲聊处理

### 6.1 触发条件

当以下条件都满足时进入闲聊模式：
1. 没有匹配到命令
2. 没有活跃会话
3. `smalltalk_mode = True`

### 6.2 闲聊提供者

通过配置选择闲聊插件：

```json
{
    "plugins": {
        "smalltalk_provider": "xiaoqing_chat"
    }
}
```

支持的提供者：
- **smalltalk**: 基于规则的简单闲聊
- **xiaoqing_chat**: 基于 LLM 的智能对话

### 6.3 xiaoqing_chat 特殊处理

当 `smalltalk_provider` 设置为 `xiaoqing_chat` 时：

- **`random_reply_rate` 不生效** - 所有群聊消息都会进入 `xiaoqing_chat` 处理
- **插件自行决定是否回复** - `xiaoqing_chat` 有自己的频率控制和回复概率判断
- **返回空列表表示不回复** - 如果插件决定不回复，返回 `[]` 即可

这样设计的原因是 LLM 模型可以根据上下文判断是否需要回复，比简单的随机概率更智能。

### 6.4 处理函数

闲聊插件需实现 `handle_smalltalk()`：

```python
async def handle_smalltalk(text: str, event: dict, context) -> List[dict]:
    """
    Args:
        text: 用户输入（已去除前缀）
        event: 原始事件
        context: 插件上下文
    
    Returns:
        回复消息段，或 None/[] 表示不回复
    """
```

---

## 7. 静音机制

### 7.1 静音命令

```
/闭嘴 30      # 静音 30 分钟
/闭嘴 1h      # 静音 1 小时
/说话         # 解除静音
```

### 7.2 静音影响

| 消息类型 | 静音时是否处理 |
|----------|---------------|
| 带命令前缀的消息 | ✅ 处理 |
| 主动 @ 机器人 | ✅ 处理命令，❌ 不闲聊 |
| 随机回复 | ❌ 不回复 |
| 定时任务 | ❌ 不发送（由插件自行判断） |

---

## 8. 并发控制与消息队列

### 8.1 概述

XiaoQing 使用多层并发控制机制来管理消息处理，确保系统稳定性和响应性能。

### 8.2 OneBot WebSocket Client 处理流程

```
┌──────────────────────────────────────────────────────┐
│  OneBot 服务器 (NapCatQQ/go-cqhttp)                  │
└─────────────────────┬────────────────────────────────┘
                      │ WebSocket 消息推送
                      ↓
┌──────────────────────────────────────────────────────┐
│  OneBotWsClient._listen()                            │
│  接收并解析 WebSocket 消息                            │
└─────────────────────┬────────────────────────────────┘
                      ↓
┌──────────────────────────────────────────────────────┐
│  第一层控制：_pending_semaphore                       │
│  最多 100 个消息等待分发（硬编码，不可配置）            │
│                                                      │
│  async with self._pending_semaphore:                │
│      await self._dispatch_event(handler, event)     │
└─────────────────────┬────────────────────────────────┘
                      ↓
┌──────────────────────────────────────────────────────┐
│  按用户/群分队列 (智能设计)                           │
│                                                      │
│  根据 queue_key 分发到不同队列：                      │
│  - group:123:user:456 → Queue1 [event1, event2]     │
│  - user:789          → Queue2 [event3]              │
│  - group:999:user:111 → Queue3 [event4, event5]     │
│                                                      │
│  每个队列有独立的 _drain_queue() 协程串行处理          │
│  保证：同一用户在同一群的消息按顺序处理                 │
│  允许：不同用户/群的消息并行处理                       │
└─────────────────────┬────────────────────────────────┘
                      ↓
┌──────────────────────────────────────────────────────┐
│  _drain_queue() → handler() → app._process_event()  │
└─────────────────────┬────────────────────────────────┘
                      ↓
┌──────────────────────────────────────────────────────┐
│  第二层控制：max_concurrency (默认 5) 🔥             │
│                                                      │
│  Dispatcher.handle_event():                         │
│  async with self.semaphore:                         │
│      return await self._process_event(event)        │
│                                                      │
│  ✅ 全局并发控制的核心                                │
│  ✅ 对所有接收方式（WS Client/Inbound）都生效         │
└─────────────────────┬────────────────────────────────┘
                      ↓
┌──────────────────────────────────────────────────────┐
│  执行命令/会话/闲聊处理并返回结果                       │
└──────────────────────────────────────────────────────┘
```

### 8.3 两层并发控制机制

#### 第一层：`_pending_semaphore` (max_pending_events = 100)
- **位置**：`core/onebot.py`
- **作用**：限制同时等待分发的事件数量
- **是否可配置**：❌ 否（硬编码）
- **影响**：仅对 OneBot WS Client 有效

#### 第二层：`max_concurrency` (默认 5) 🔥
- **位置**：`core/dispatcher.py`
- **作用**：限制同时执行处理逻辑的数量（**真正的并发控制**）
- **是否可配置**：✅ 是（`config.json`）
- **影响**：全局生效（WS Client、Inbound Server）

### 8.4 按用户/群分队列设计

**核心设计**：每个 `(group_id, user_id)` 组合对应一个独立队列。

**queue_key 生成规则**：
```python
# 群聊消息
queue_key = f"group:{group_id}:user:{user_id}"

# 私聊消息
queue_key = f"user:{user_id}"
```

**优势**：
1. ✅ **保证顺序**：同一用户在同一群的消息严格按顺序处理
2. ✅ **提高吞吐**：不同用户/群的消息可以并行处理
3. ✅ **避免阻塞**：某个用户的慢操作不影响其他用户

**实际运行示例**：

假设 `max_concurrency = 5`，同时收到如下消息：

| 时间 | 来源 | queue_key | 状态 |
|------|------|-----------|------|
| T1 | 群A用户1 | `group:A:user:1` | ✅ 获得第1个并发槽 |
| T2 | 群A用户1 | `group:A:user:1` | ⏳ 在队列中等待（同一用户串行） |
| T3 | 群B用户2 | `group:B:user:2` | ✅ 获得第2个并发槽 |
| T4 | 群C用户3 | `group:C:user:3` | ✅ 获得第3个并发槽 |
| T5 | 群D用户4 | `group:D:user:4` | ✅ 获得第4个并发槽 |
| T6 | 群E用户5 | `group:E:user:5` | ✅ 获得第5个并发槽 |
| T7 | 群F用户6 | `group:F:user:6` | ⏸️ **等待并发槽释放** |
| T8 | 群A用户1 | `group:A:user:1` | ⏳ 在队列中等待（排在 T2 后面） |

### 8.5 Inbound WebSocket Server 队列机制

对于 Inbound Server（被动接收推送），有额外的队列机制：

```
WebSocket 消息到达
    ↓
放入队列 (maxsize = ws_queue_size, 默认 200)
    ↓
inbound_ws_max_workers 个 worker 从队列取消息 (默认 8 个)
    ↓
通过 Semaphore 获取处理许可 (max_concurrency, 默认 5)
    ↓
处理消息 (Dispatcher)
```

**配置参数**：

| 参数 | 默认值 | 作用范围 | 说明 |
|------|--------|----------|------|
| `ws_queue_size` | 200 | Inbound + OneBot | 等待处理的消息队列长度 |
| `inbound_ws_max_workers` | 8 | Inbound Server | 并发处理队列消息的 worker 数 |

⚠️ **注意**：`inbound_ws_max_workers` 仅对 Inbound WS Server 有效；`ws_queue_size` 同时影响 Inbound WS Server 与 OneBot WS Client。

### 8.6 核心配置参数

| 参数 | 默认值 | 适用范围 | 说明 |
|------|--------|----------|------|
| **`max_concurrency`** | 5 | 全局 | 🔥 最重要！全局并发控制 |
| `inbound_ws_max_workers` | 8 | Inbound Server | Worker 协程数 |
| `ws_queue_size` | 200 | Inbound + OneBot | 队列长度（0 表示不限制） |

### 8.7 配置建议

#### 低负载场景（个人使用，1-3 个群）
```json
{
  "max_concurrency": 5
}
```

#### 中等负载（多个活跃群组）
```json
{
  "max_concurrency": 10,
  "ws_queue_size": 300,
  "inbound_ws_max_workers": 12
}
```

#### 高负载场景（大量群组，频繁消息）
```json
{
  "max_concurrency": 20,
  "ws_queue_size": 500,
  "inbound_ws_max_workers": 24
}
```

**优化原则**：
1. `inbound_ws_max_workers >= max_concurrency`（避免 worker 空闲）
2. `ws_queue_size` 足够大以吸收突发流量（或设为 0 不限制）
3. `max_concurrency` 不要设置过高，避免资源耗尽

### 8.8 性能监控

查看队列状态：
```
GET /health
```

响应示例：
```json
{
  "status": "ok",
  "ws_connections": 1,
  "pending_jobs": 3,
  "active_sessions": 2
}
```

---

## 9. 完整处理流程图

```
┌─────────────────────────────────────────────────────────────────┐
│                     OneBot 消息事件到达                          │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
                    ┌─────────────────┐
                    │ post_type ==    │
                    │  "message" ?    │
                    └────────┬────────┘
                             │
                    ┌────────┴────────┐
                    │                 │
                   Yes               No ──────────────────► 忽略
                    │
                    ▼
        ┌───────────────────────┐
        │   normalize_message   │
        │ 提取 text, user_id,   │
        │      group_id         │
        └───────────┬───────────┘
                    │
                    ▼
        ┌───────────────────────┐
        │   URL 检测            │──── 有 URL 且无前缀 ────► url_parser
        └───────────┬───────────┘
                    │
                    ▼
        ┌───────────────────────┐
        │   _make_decision     │
        │   判断是否处理消息     │
        └───────────┬───────────┘
                    │
           ┌────────┴────────┐
           │                 │
    should_process        不处理 ──────────────────────► 返回 []
        = True
           │
           ▼
    ┌──────────────────────────────────────────────────────────┐
    │                  Handler 链式处理                          │
    │                   按优先级依次尝试                          │
    └──────────────────────────────────────────────────────────┘
                    │
           ┌────────┴────────┐
           │                 │
        BotNameHandler      失败
        仅机器人名字?         │
           │                 │
      ┌────┴────┐            ▼
     Yes       No    ┌─────────────────┐
      │        │     │ CommandHandler  │
      ▼        │     │  命令匹配?       │
  处理完成     │     └────────┬────────┘
  _handle_bot  │              │
    _name_only  │      ┌───────┴───────┐
                │   匹配成功         未匹配
                │      │              │
                │      ▼              ▼
                │  ┌───────────┐  ┌─────────────────┐
                │  │ 权限检查   │  │ SessionHandler  │
                │  └─────┬─────┘  │  活跃会话?       │
                │        │        └────────┬────────┘
                │        ▼         ┌────────┴────────┐
                │  ┌───────────┐ 有会话          无会话
                │  │ 执行命令   │    │              │
                │  │ handler() │    ▼              ▼
                │  └─────┬─────┘  ┌──────────┐  ┌─────────────────┐
                │        │        │handle_   │  │ SmalltalkHandler│
                │        │        │session() │  │  smalltalk_mode? │
                │        │        └─────┬────┘  └────────┬────────┘
                │        │              │       ┌────────┴────────┐
                │        │              │      Yes              No
                │        │              │       │                │
                │        │              │       ▼                ▼
                │        │              │  ┌───────────┐      返回 []
                │        │              │  │_handle_   │
                │        │              │  │smalltalk() │
                │        │              │  └─────┬─────┘
                │        │              │        │
                └────────┴──────────────┼────────┴────────┘
                                       │
                                       ▼
                              ┌─────────────────┐
                              │ 返回消息段列表   │
                              └─────────────────┘
```

---

## 附录：关键代码位置

| 功能 | 文件 | 函数/方法 |
|------|------|----------|
| 消息解析 | `core/message.py` | `normalize_message()`, `extract_text()` |
| 决策判断 | `core/dispatcher.py` | `_make_decision()` |
| 前缀剥离 | `core/dispatcher.py` | `_strip_prefix()` |
| 命令路由 | `core/router.py` | `CommandRouter.resolve()` |
| 会话管理 | `core/session.py` | `SessionManager` |
| 闲聊处理 | `core/dispatcher.py` | `_handle_smalltalk()` |
| 静音控制 | `core/dispatcher.py` | `mute_group()`, `is_muted()` |
| Handler 链 | `core/dispatcher.py` | `BotNameHandler`, `CommandHandler`, `SessionHandler`, `SmalltalkHandler` |
