# 📋 05 - API 参考

本章把插件开发常用 API 集中放在一起，便于写插件时随手查。

---

## 🛠️ plugin_base 模块

导入方式如下。
```python
from core.plugin_base import (
    text, image, image_url, record, record_url,
    segments, build_action, bounded_external_text, run_sync,
    ensure_dir, load_json, write_json, atomic_write_text,
    split_message_segments,
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

手写消息段时，推荐使用 `Path(file_path).resolve().as_uri()` 生成本地文件 URI。

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

同样地，手写本地语音消息段时也优先使用 `Path(file_path).resolve().as_uri()`。

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

#### bounded_external_text(value, *, max_chars, max_bytes, ...)

把第三方标量收窄成可展示文本：拒绝容器、布尔值和非有限浮点数，剥离 ANSI 与 C0/C1 控制字符，并同时满足字符数和 UTF-8 字节数上限。协议字段不能接受半截值时传 `truncate=False`，会改用 `default`。

```python
safe_name = bounded_external_text(
    remote_payload.get("name"),
    max_chars=128,
    max_bytes=512,
    default="未知",
    truncate=False,
)
```

### 异步工具

#### run_sync(func, *args, **kwargs)
经当前插件的有界同步 bulkhead 运行同步函数，避免阻塞事件循环，同时遵守单插件并发/排队限制、跨插件公平调度和 unload/reload 隔离。

```python
import requests

async def handle(...):
    # 避免阻塞事件循环
    response = await run_sync(requests.get, "https://api.example.com")
    return segments(response.text)
```

当插件或全局同步队列已满时，该调用会快速失败，不会无界积压。取消尚未启动的调用会从队列移除；若函数已经进入线程，取消不能强制停止 Python 代码，框架会继续持有并跟踪真实 future，关闭时有界 drain，必要时隔离旧插件代。普通插件不得以 `asyncio.to_thread()` 绕过该路径；自管线程仅适用于拥有专用有界 executor 和完整 `init`/`shutdown` 生命周期的底层组件。

### image_validation 模块

网络图片不得只信 URL、MIME、扩展名或 Pillow header。用 `validate_image_bytes()` 同时校验字节、像素、帧数、格式/声明一致性、容器终止边界、解压炸弹，并在 `verify()` 后重新打开逐帧真实解码。本地缓存用 `validate_image_path()`；它还拒绝符号链接和硬链接，并以 `lstat → O_NOFOLLOW open → fstat/lstat → 读取后复核` 防止 check/open 竞态。

```python
from core.image_validation import ImageValidationLimits, validate_image_bytes

validated = await run_sync(
    validate_image_bytes,
    response.body,
    limits=ImageValidationLimits(
        max_bytes=8 * 1024 * 1024,
        max_pixels=20_000_000,
        max_frames=120,
    ),
    expected_format="PNG",
)
filename = f"{digest}{validated.extension}"
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
写入 JSON 文件（先写临时文件再替换，防止中断损坏）。

```python
write_json(Path("data.json"), {"count": 1})
```

#### atomic_write_text(path, payload)
原子写入文本文件。

```python
atomic_write_text(Path("output.txt"), "内容")
```

### 消息分割

#### split_message_segments(segs, max_length=500)
将消息段列表按文本长度分割为多个分片，防止超长消息被 OneBot 截断。

```python
long_segs = [text("很长的内容...")]
parts = split_message_segments(long_segs, max_length=500)
# parts = [[seg1, seg2], [seg3, ...], ...]
```

---

## PluginContext 类

插件处理函数的 `context` 参数类型。

### 属性

| 属性 | 类型 | 说明 |
|------|------|------|
| `config` | `Mapping[str, Any]` | 构造期的插件作用域只读视图；运行时配置读取使用 `get_settings_snapshot()` |
| `secrets` | `Mapping[str, Any]` | 构造期的插件秘密作用域只读视图；运行时配置读取使用 `get_settings_snapshot()` |
| `plugin_name` | `str` | 当前插件名 |
| `plugin_dir` | `Path` | 插件目录路径 |
| `data_dir` | `Path` | 数据目录路径 |
| `logger` | `_RequestLogger` | 日志记录器（自动附带 request_id） |
| `http_session` | `aiohttp.ClientSession \| None` | HTTP 客户端 |
| `send_action` | `Callable` | 发送 OneBot Action 的异步回调，可用于后台任务完成后的主动通知 |
| `metrics` | `MetricsCollector \| None` | 运行指标收集器 |
| `current_user_id` | `int \| None` | 当前消息的用户 ID |
| `current_group_id` | `int \| None` | 当前消息的群 ID |
| `state` | `Dict[str, Any]` | 插件私有状态（当次请求生命周期） |
| `principal` | `PluginPrincipal` | 核心签发的当前调用身份 |
| `capabilities` | `PluginCapabilities` | 当前插件与调用身份获授权的窄能力集合 |
| `command_invocation` | `CommandInvocation \| None` | 当前命令的递归目录匹配结果；生命周期、schedule 和非命令入口为 `None` |

### 方法

#### get_settings_snapshot()

返回一个不可变的 `PluginSettingsSnapshot`，其中公开配置、秘密和 `revision` 来自同一次原子发布。`plugin_config(context.plugin_name)` 与 `plugin_secrets(context.plugin_name)` 提取当前插件命名空间；插件看不到其他插件的秘密命名空间。

```python
settings = context.get_settings_snapshot()
config = settings.plugin_config(context.plugin_name)
secrets = settings.plugin_secrets(context.plugin_name)
revision = settings.revision
```

同一个业务操作需要多个设置时只获取一次快照。长期管理器用 `revision` 拒绝过期回调，并在校验整组候选设置后一次发布。

#### get_config(path) / get_secret(path)
分别读取当前插件命名空间中的配置或秘密，并返回分离副本；不存在时返回 `None`。路径使用
点号分隔，例如 `context.get_secret("provider.api_key")`。插件无法借此读取其他插件或全局秘密。它们各自读取调用时的当前代；若多个值必须保持同代，请使用 `get_settings_snapshot()`。

#### is_global_admin(user_id=None)
仅在待检查用户等于核心签发 principal 的当前用户，且当前 capability 仍标记为机器人管理员时
返回 `True`。它不会暴露管理员 ID 列表，也不能用来检查任意第三方用户。

#### capabilities.ai

通过当前插件自己的命名 route 调用统一 LLM/VLM 注册表。统一 provider 密钥不会进入 `context.secrets`。

```python
result = await context.capabilities.ai.complete(
    "summary",
    [{"role": "user", "content": "请总结这段内容"}],
    required_modalities=("text",),
    temperature=0.3,
    max_tokens=400,
)

text = result.content
profile = result.profile
attempts = result.attempts
```

`complete()` 的可选关键字包括：

| 参数 | 说明 |
|------|------|
| `required_modalities` | 请求所需模态；视觉请求使用 `("text", "image")` |
| `pinned_model` | 严格固定到 route 内的一个 profile，不跨模型 fallback |
| `temperature` / `top_p` / `max_tokens` | 覆盖 route 的任务级生成参数 |
| `timeout_seconds` / `total_timeout_seconds` | 单次尝试和整条链的超时 |
| `max_retry` / `retry_interval_seconds` | 每个模型的重试控制 |
| `tools` / `tool_choice` | OpenAI-compatible 工具调用字段 |
| `extra_payload` | 服务商特有的非保留请求字段；不能覆盖 model/messages/采样参数/工具字段 |

`list_models(route, required_modalities=...)` 返回 route 的有序 `AIModelInfo` 元组，只含 `name/provider/model/modalities`，不含 URL、代理或密钥。模型、provider、route 以及凭据的配置格式见 [06-configuration.md](06-configuration.md#统一-aivlm-注册表)。

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

#### get_command_catalog()
获取当前已发布插件代的完整、不可变、结构化命令目录。

```python
roots = context.get_command_catalog()
for root in roots:
    for node in root.walk():
        print(node.code, node.usage)
```

每个 `CommandCatalogNode` 都包含 `code`、`plugin`、`path`、`name`、`aliases`、
`help_text`、`usage`、`match_mode`、`permission`、`contexts`、`examples`、
`invalid_examples` 和 `children`。`to_dict()` 返回可直接 JSON 序列化、且不包含处理器的
公开视图。`/help` 默认只渲染插件级功能导航，`/help <插件名>`
渲染该插件的递归命令目录；自动化应读取 `/help json page N`，不要从
任何 `/help` 格式化文本反向解析命令。

#### command_invocation

命令请求中，Dispatcher 会注入 `CommandInvocation`：

```python
invocation = context.command_invocation
if invocation is not None:
    print(invocation.root.code)       # 顶层稳定码
    print(invocation.node.code)       # 最深命中节点
    print(invocation.arguments)       # 最深节点后的业务参数
    print(invocation.remainder_after(1))
```

复杂插件可调用 `core.router.resolve_context_command_invocation(context, root_code, args)`；
它优先复用 Dispatcher 的解析结果，直接单测调用时再从同一目录快照解析。

#### list_plugins()
获取所有已加载插件。

```python
plugins = context.list_plugins()  # -> ["core", "echo", ...]
```

#### send_action(action)
主动发送 OneBot Action。

普通命令处理通常只需要 `return segments(...)`，由框架自动构建并发送响应。只有后台任务、定时任务或需要在当前 handler 返回之后再通知用户时，才直接调用 `context.send_action(action)`。

```python
from core.plugin_base import build_action, segments

action = build_action(
    segments("[codex:main #1] 完成:\n结果内容"),
    user_id=event.get("user_id"),
    group_id=event.get("group_id"),
)
if action:
    await context.send_action(action)
```

长文本不需要在插件里手动截断；发送链路会通过 `split_message_segments()` 按文本长度分片。

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
- `initial_data` - 初始会话数据；必须是字符串键的有界 JSON-like 值树（内建
  `dict/list/tuple` 与 `str/bytes/int/float/bool/None`），不接受自定义对象或循环引用
- `timeout` - 超时时间（秒）

**返回**：新会话的隔离 `Session` 快照。快照包含稳定的 `session_id`，但修改快照不会写回。

#### get_session()
获取当前用户会话的隔离快照，并刷新空闲超时。

```python
session = await context.get_session()
if session:
    step = session.get("step")
```

**返回**：`Session` 快照或 `None`（无会话或已过期）。持久修改必须使用
`update_session()`，不能直接修改此快照。

#### update_session(callback)

对当前会话执行原子读改写。callback 收到私有工作副本，可为同步函数或直接调用得到的
coroutine；同步和异步 callback 都在框架自建任务中运行并经过提交前取消检查。成功时版本只
增加一次，异常、取消和值树校验失败均不提交。callback 不得返回已调度的 `asyncio.Task` 或
裸 `Future`（误返对象会被取消并 drain），也不能对同一会话嵌套调用 `update_session()`。

```python
async def advance(working):
    working.set("step", working.get("step", 0) + 1)
    await do_other_work()

await context.update_session(advance)
```

#### end_session()
结束当前用户的会话。

```python
await context.end_session()
```

**返回**：`bool` - 是否成功删除

#### has_session()
检查当前用户是否有活跃会话，不刷新空闲超时。

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
获取剩余静音时间（分钟）。

```python
remaining = context.get_mute_remaining(123456)  # -> 15.5（分钟）
```

**返回值**：`float` - 剩余静音时间（分钟），0 表示未静音

---

## Session 类

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

## Dispatcher 线性消息流程

Dispatcher 的入口由 `MessageParser.parse()` 构建 `MessageContext`，随后 `_process_event()` 按固定 A-G 顺序处理。插件通过命令、会话、smalltalk provider 等约定函数接入。

### MessageContext 关键字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `clean_text` | `str` | 去除开头 bot_name / 命令前缀后的文本 |
| `has_bot_name` | `bool` | 原始文本任意位置包含 `bot_name` |
| `has_command_prefix` | `bool` | 原始文本严格以命令前缀开头 |
| `has_prefix` | `bool` | `has_command_prefix`、`has_bot_name`、`is_at_me` 的并集 |
| `is_only_bot_name` | `bool` | 只叫机器人名字或只 @ 机器人 |
| `is_at_me` | `bool` | OneBot at 段或 raw_message 中 @ 机器人 |
| `is_url_only` | `bool` | `clean_text.strip()` 整体匹配 `^https?://\S+$` |
| `is_empty` | `bool` | 无文本、媒体或 @；仅允许活跃会话消费 |

### 处理顺序

```
Step A: 处理门控（私聊、require_bot_name_in_group=False、has_prefix、活跃 session；先 resolve 再按类别 observe）
Step B: is_url_only → url_parser（在门控与静音之后；静音时跳过）
Step C: is_only_bot_name → 默认回应 / call_bot_name_only
Step D: 活跃 session → 转 session 插件
Step E: router 命中 → 执行命令
Step F: has_command_prefix 且命令未命中且首字母为字母 → 未知命令提示
Step G: 回落 smalltalk provider（mute 仅在此步及普通群闲聊阻塞）
```

### URL 与未知命令

- URL 处理改用 `ctx.is_url_only`，只接受完整单 URL。`看看 https://example.com` 不会触发 `url_parser`。
- 未知命令提示只在 `has_command_prefix=True` 且 router 未命中时出现。`小青 不存在的指令` 会继续走会话或 smalltalk 回落，不会被当作 `/不存在的指令`。

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
| `data` | `Dict[str, SessionValue]` | 有界 JSON-like 会话数据；不接受自定义对象或循环引用 |
| `timeout` | `float` | 超时时间（秒） |

### 方法

#### get(key, default=None)
获取会话数据。

```python
step = session.get("step", 1)
```

#### set(key, value)
修改 `update_session()` callback 收到的工作副本。不要在 `get_session()` 返回的读取快照上
调用它来尝试持久化；快照修改不会写回。

```python
def update_progress(working):
    working.set("step", 2)
    working.set("attempts", working.get("attempts", 0) + 1)

await context.update_session(update_progress)
```

#### clear()
清空 `update_session()` callback 收到的工作副本。

```python
await context.update_session(lambda working: working.clear())
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

当用户处于活跃会话时，此函数会被调用处理用户的后续消息。在 dispatcher 线性流程中，Step D 负责调用此函数。

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
Dispatcher Step F 捕获
    ↓
调用 handle_session()
    ↓
插件处理并返回回复
    ↓
原子更新（context.update_session(callback)）
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
    await context.update_session(lambda working: working.set("attempts", attempts))
    
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

在 dispatcher 线性流程中：

1. **模态优先级**：会话处理先于全局命令；处理器明确返回 `None` 时才回落到命令匹配
2. **绕过普通触发条件**：群聊普通文本没有 `has_prefix` 时，只要活跃会话存在仍会处理
3. **独立处理**：会话处理不依赖 bot_name；只有 `is_only_bot_name` 会先走只叫名字回应，避免打断“叫机器人”语义
4. **空白输入**：无会话时丢弃，有会话时交给处理器，用于表单默认值等语义

### 注意事项

1. **会话超时**
   - 超过 `timeout` 时间后，会话自动过期
   - 下一次读取会清理过期项并返回空；只有插件再次调用 `create_session()` 才会创建新会话

2. **每个用户独立**
   - 每个 `(user_id, group_id)` 组合有独立的会话
   - 私聊和群聊的会话互不影响

3. **手动结束**
   - 游戏或对话结束时，必须调用 `context.end_session()`
   - 否则用户需要等待超时才能开始新会话

4. **数据更新**
   - 只有 `context.update_session(callback)` 成功提交才会持久化 callback 中的 `set/delete/clear`
   - 成功提交会刷新 `updated_at` 并延长会话有效期；读取快照上的修改不会写回

---

## handle_url() 函数签名

URL 自动解析函数（可选）。

Dispatcher 只在 Step A 调用此函数：`ctx.clean_text.strip()` 必须整体匹配 `^https?://\S+$`。含附加文字或多个 URL 的消息不会进入 `handle_url()`。

```python
async def handle_url(
    url: str,               # clean_text 中的完整单 URL
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

1. **进入 smalltalk 回落时会调用此函数**
   - 由插件内部控制 attention、回复频率、普通插话概率、PFC planner 和 reply checker

2. **插件可以自主决定是否回复**
   - 返回 `None` 或 `[]` 表示不回复
   - 返回消息段列表表示需要回复

3. **xiaoqing_chat 的 directed attention 会跳过普通概率门**
   - `/xc`、私聊、`@`、直接叫名字、只喊名字后的追问、reply 引用小青、以及有近期上下文锚点的“她/ta”共指召唤会走 forced 路径
   - 普通群聊消息才走 `reply_probability_base`、heartflow 和硬频控

4. **可以实现更复杂的逻辑**
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
      "api_base": "https://api.example.com"
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

## core.args 模块

```python
from core.args import parse, ParsedArgs
```

#### parse(raw) -> ParsedArgs
解析命令参数字符串，返回 `ParsedArgs` 对象。

```python
parsed = parse("add 完成报告 --cat=工作 -p 2")
```

#### ParsedArgs

| 属性/方法 | 说明 |
|-----------|------|
| `parsed.first` | 第一个位置参数（property） |
| `parsed.second` | 第二个位置参数（property） |
| `parsed.get(i, default="")` | 获取第 i 个位置参数 |
| `parsed.rest(start=0)` | 从第 start 个参数开始拼接剩余参数 |
| `parsed.opt(key, default="")` | 获取选项值（`--key=val` 或 `-k val`） |
| `parsed.has(key)` | 检查选项/标志是否存在 |
| `len(parsed)` | 位置参数数量 |
| `bool(parsed)` | 参数字符串是否非空 |
| `parsed.raw` | 原始参数字符串 |
| `parsed.tokens` | 位置参数列表 |
| `parsed.options` | 选项字典 |

---

## ➡️ 下一步

- 配置详解 → [06-configuration.md](06-configuration.md)
- 高级主题 → [07-advanced.md](07-advanced.md)
