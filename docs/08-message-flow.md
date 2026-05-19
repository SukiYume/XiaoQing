# 📨 08 - 消息处理流程

本章沿着一条消息的生命周期说明处理逻辑，覆盖接收、路由、命令解析、会话管理和发送流程。

> [!TIP]
> 排查消息不回复时，可以先看 [9️⃣ 完整处理流程图](#9️⃣-完整处理流程图)，再回到对应章节找细节。

---

## 📑 目录

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

## 1️⃣ 消息处理总览

XiaoQing 的消息处理由以下核心模块协作完成。

| 模块 | 文件 | 职责 |
|------|------|------|
| **Dispatcher** | `core/dispatcher.py` | 消息分发器，协调整个处理流程 |
| **CommandRouter** | `core/router.py` | 命令路由，匹配触发词并拆分参数 |
| **SessionManager** | `core/session.py` | 会话管理，支持多轮对话 |
| **message** | `core/message.py` | 消息解析工具函数 |

消息链路的核心原则是“框架判断入口，插件判断业务”。框架负责把 OneBot 事件规范化、生成统一的 `MessageContext`、执行 URL 短路、门控、命令匹配、会话恢复和 smalltalk 回落；插件负责自己的业务语义。典型例子如下。

- `pendo` 的日程、待办、账本、Web 和提醒逻辑全部在插件内完成，框架只负责把 `/pendo ...` 和定时任务调过去。
- `xiaoqing_chat` 作为 `smalltalk_provider` 时，框架把群聊消息交给插件观察；插件内部再用 attention gate、频控、planner、LLM 和 reply checker 决定是否回复。

### 1.1 处理流程概述

```
OneBot 事件
    ↓
Dispatcher.handle_event()
    ↓
┌─────────────────────────────────────────────────────────────┐
│  1. 事件类型检查（仅处理 message 类型）                      │
│  2. 消息解析（提取文本、user_id、group_id）                  │
│  3. URL-only 短路（clean_text 为单个 URL）                   │
│  4. 处理门控（私聊 / 配置放行 / has_prefix / 活跃会话）       │
│  5. 线性分发：只喊名字 → 命令 → 未知命令 → 会话 → 闲聊       │
└─────────────────────────────────────────────────────────────┘
    ↓
返回 OneBot 消息段列表
```

---

### 1.2 线性分发流程

`Dispatcher._process_event()` 按固定 A-G 顺序执行：

```
Step A: URL short-circuit（clean_text 单 URL → url_parser；mute 不影响）
Step B: 处理门控
        - 私聊：处理
        - require_bot_name_in_group=False：处理
        - has_prefix（/ 开头 OR bot_name OR @me）：处理
        - 有活跃 session 且非 is_only_bot_name：处理
        - 否则：丢弃
Step C: is_only_bot_name → 默认回应 / call_bot_name_only
Step D: router 命中 → 执行命令
Step E: has_command_prefix 且命令未命中 → 未知命令提示
Step F: 活跃 session → 转 session 插件
Step G: 回落 smalltalk provider（mute 仅在此步阻塞）
```

`xiaoqing_chat` 作为 `smalltalk_provider` 时，dispatcher 会先调用 `observe_message()` 让插件观察消息；只有消息通过门控并落到 Step G 时才调用 `handle_smalltalk()`。插件内部继续负责 attention gate、硬频控、普通插话概率、heartflow、PFC planner 和 reply checker。

---

## 2️⃣ 消息接收与解析

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

`MessageParser.parse()` 从事件中提取关键信息并生成 `MessageContext`：

```python
ctx = MessageParser(config_provider).parse(event)
# ctx.text: "/help 查看帮助"
# ctx.clean_text: "help 查看帮助"
# ctx.user_id: 123456789
# ctx.group_id: 987654321 (私聊时为 None)
# ctx.has_command_prefix: True
# ctx.has_prefix: True
```

### 2.3 文本提取

`extract_text()` 函数从 OneBot 消息段中提取纯文本：

- **字符串消息**: 直接返回
- **消息段数组**: 提取所有 `type: "text"` 段的文本并拼接
- 其他类型（图片、@等）: 不参与命令文本提取

> 说明：命令路由仍然只看纯文本；但当 `smalltalk_provider = "xiaoqing_chat"` 且插件媒体能力开启时，纯图片/表情包消息不会在 parser 阶段被丢弃，后续会由 `xiaoqing_chat` 自己读取原始消息段并渲染成 `[图片：...]` / `[表情包：...]` 注入上下文。

> 补充：进入 `xiaoqing_chat` 后，插件会按原始消息段顺序把文本和媒体 marker 重新拼回“有效用户输入”。也就是说，`文字 + 图片 + 文字` 不会再塌成“所有文字在前、所有图片在后”。

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

## 3️⃣ 触发条件判断

### 3.1 处理门控

Dispatcher 在 URL-only 短路之后进入 Step B。满足以下任一条件时继续处理：

| 条件 | 说明 |
|------|------|
| 私聊 | 私聊消息始终处理 |
| `require_bot_name_in_group=False` | 群聊普通文本也进入后续流程 |
| `has_prefix=True` | 命令前缀、bot_name 或 @me 任一信号命中 |
| 活跃会话 | 用户处于 session，且当前消息不是 `is_only_bot_name` |

否则直接返回 `[]`。`random_reply_rate` 不参与 dispatcher 分发，群聊回复概率由 smalltalk provider 自己决定。

### 3.2 `has_prefix` 与 `has_command_prefix`

`has_prefix` 表示消息以命令前缀（默认 `/`）开头，或包含 `bot_name`（任意位置），或包含 @机器人（任意位置）。`has_command_prefix` 单独标识严格以命令前缀开头。

这两个字段的区别影响两个行为：

- 处理门控使用 `has_prefix`，所以 `你好啊小青` 和 @me 消息都能进入后续流程。
- 未知命令提示只使用 `has_command_prefix`，所以 `小青 不存在的指令` 不会被提示成未知 `/不存在的指令`。

### 3.3 配置项

```json
{
    "bot_name": "小青",
    "command_prefixes": ["/"],
    "require_bot_name_in_group": true
}
```

- **bot_name**: 机器人名称，任意位置出现都会让 `has_prefix=True`
- **command_prefixes**: 命令前缀列表，通常为 `["/"]`
- **require_bot_name_in_group**: 群聊是否需要 @、提及 bot_name 或命令前缀

### 3.4 前缀剥离

`parse_text_command_context()` 函数按照以下顺序严格处理前缀剥离：

1. **去除 @机器人**（例如 `[CQ:at,qq=123] `）
2. **去除 bot_name**（例如 `小青`，支持模糊匹配及其后的标点）
3. **去除 command_prefixes**（例如 `/`）

**⚠️ 重要解析规则：**

当用户输入 **`小青配置`** 时：
1. `bot_name`（小青）首先被检测并移除，剩余文本变为 **`配置`**。
2. 随后尝试移除命令前缀（如 `/`），因不匹配而跳过。
3. 最终传递给 Router 的文本是 **`配置`**。

插件命令触发词定义为 `["小青配置"]` 时会**匹配失败**，因为 Router 看到的是 `"配置"`。

**建议做法**
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

## 4️⃣ 命令路由与参数拆分

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

## 5️⃣ 会话管理

### 5.1 会话触发

会话处理在 Step F 执行，位于命令匹配和未知命令提示之后、smalltalk 回落之前：

```python
# Step F 处理逻辑
session = await session_manager.get(user_id, group_id)
if session:
    # 路由到会话插件的 handle_session()
```

**重要特性**：
- **优先级**：会话处理在命令之后、闲聊之前
- **绕过普通触发条件**：群聊普通文本没有 `has_prefix` 时，只要活跃会话存在仍会处理
- **只喊名字优先**：`is_only_bot_name` 先走 Step C，不会被活跃会话抢走

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

### 5.5 与后台队列的区别

框架 Session 会接管同一用户后续未命中命令的消息，适合交互式表单、游戏、SSH REPL 和 `/pendo ledger add` 这种逐步引导。耗时后台任务不应为了“保持上下文”而创建框架 Session。

`codex` 插件使用独立业务会话：`/codex create main` 只创建 Codex 标签和工作目录，后续必须显式发送 `/codex main <任务>`。任务进入插件自己的队列后，当前 handler 立即返回；完成结果通过 `context.send_action()` 主动发送，图片结果会以 QQ image 段随文字回发，因此不会影响用户继续发其他命令或闲聊。

`arxiv_filter` 触发 Codex 摘要时也遵循同一消息流：`/arxiv` 的 handler 先返回论文列表，随后后台侧路把链接投递给 `astro-ph`。Codex 摘要完成后再主动发送第二条消息；如果摘要失败，失败消息也在这条后台链路里发送，不会回滚或阻塞第一条论文列表。

---

## 6️⃣ 闲聊处理

### 6.1 触发条件

当以下条件都满足时进入 smalltalk 回落：
1. 没有匹配到命令
2. 没有活跃会话
3. 消息已经通过 Step B 处理门控
4. 当前群没有被静音

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

- **`random_reply_rate`** - 兼容保留字段，不参与 dispatcher 分发
- **插件自行决定是否回复** - `xiaoqing_chat` 有自己的 attention gate、频率控制和普通插话概率判断
- **directed attention 会强制回复** - 一旦进入插件，`/xc`、私聊、`@`、直接叫 `bot_name`、只喊名字后的追问、reply 引用小青，或有近期上下文锚点的“她/ta”共指召唤会走 forced 路径
- **返回空列表表示不回复** - 插件决定不回复时返回 `[]`
- **图片消息可进入闲聊链** - 纯图片/表情包消息会被保留到插件层，再决定是否写入上下文
- **混合图文会保留顺序** - `xiaoqing_chat` 会在插件层按原始 segment 顺序重建有效用户输入
- **媒体回复是后处理步骤** - 主回复仍先生成纯文本，是否再补本地图片 / 表情包 / QQ 表情由插件第二阶段决定；旧图库坏条目则在后台补修

这样设计的原因是 LLM 模型可以根据上下文判断回复时机，比 dispatcher 固定随机概率更贴近实际对话。

### 6.4 处理函数

闲聊插件需实现 `handle_smalltalk()`：

```python
async def handle_smalltalk(text: str, event: dict, context) -> List[dict]:
    """
    Args:
        text: 用户输入（已去除前缀；xiaoqing_chat 可能再结合 event 中的图片段生成有效上下文）
        event: 原始事件
        context: 插件上下文
    
    Returns:
        回复消息段，或 None/[] 表示不回复
    """
```

---

## 7️⃣ 静音机制

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

## 8️⃣ 并发控制与消息队列

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

查看运行状态：
```
GET /health
```

如果配置了 `inbound_token`，需要携带：
```
Authorization: Bearer <inbound_token>
```

响应示例：
```json
{
  "status": "ok",
  "ws_connections": 1,
  "plugins_loaded": 29,
  "pending_jobs": 3,
  "active_sessions": 2
}
```

查看详细指标：
```
GET /metrics
```

`/metrics` 返回 `MetricsCollector` 聚合的插件执行统计；`/health` 与 `/metrics` 都由 Inbound HTTP 服务直接提供。

### 8.9 出站发送路径

所有插件最终都走统一的 `_send_action()` 发送链路。

1. 优先复用当前事件上下文中的 action sink。
2. 尝试发送到 OneBot WS Client。
3. 若当前启用了 Inbound WS 且有活跃客户端，则广播给活跃客户端。
4. 若仍不可用，则回退到 OneBot HTTP sender。

因此启用双通道部署时，WS 短暂断开不会直接导致消息静默丢失。

后台任务也应复用这条发送链路。插件可以保存触发任务时的 `user_id` / `group_id`，等任务完成后通过 `context.send_action(build_action(...))` 主动发送。发送阶段仍会执行长文本分片、WS/HTTP 回退和错误日志处理；插件自身不需要为了避免截断而设置单独的结果字符上限。

---

## 9️⃣ 完整处理流程图

```
OneBot 消息事件
    ↓
post_type == "message" ? ── 否 → 忽略
    ↓ 是
MessageParser.parse()
    ↓
observe_message()
    ↓
Step A: ctx.is_url_only ?
    ├─ 是 → _invoke_url_parser()
    └─ 否
        ↓
Step B: 处理门控
    ├─ 私聊
    ├─ require_bot_name_in_group=False
    ├─ has_prefix
    └─ 活跃 session
        ↓
Step C: is_only_bot_name ? ── 是 → _handle_bot_name_only()
        ↓ 否
Step D: router.resolve(clean_text) 命中 ? ── 是 → _execute_command()
        ↓ 否
Step E: has_command_prefix ? ── 是 → 未知命令提示
        ↓ 否
Step F: 活跃 session ? ── 是 → _try_handle_session()
        ↓ 否
Step G: 群静音 ? ── 是 → 返回 []
        ↓ 否
_handle_smalltalk()
```

---

## 📎 附录：关键代码位置

| 功能 | 文件 | 函数/方法 |
|------|------|----------|
| 消息解析 | `core/message.py` | `parse_text_command_context()`, `extract_text()` |
| 前缀剥离 & 上下文解析 | `core/message.py` | `parse_text_command_context()` |
| 线性分发 | `core/dispatcher.py` | `Dispatcher._process_event()` |
| 命令路由 | `core/router.py` | `CommandRouter.resolve()` |
| 会话管理 | `core/session.py` | `SessionManager` |
| 静音控制 | `core/dispatcher.py` | `mute_group()`, `is_muted()` |
| URL-only 路由 | `core/dispatcher.py` | `Dispatcher._invoke_url_parser()` |
