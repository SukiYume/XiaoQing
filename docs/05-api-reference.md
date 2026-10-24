# 🔌 05 - API 参考

中文 | [English](05-api-reference.en.md)

本章汇总插件开发常用的公开类型、函数和入口签名。
开发流程见 [插件开发指南](03-plugin-development.md)，实现所有权见 [Core 模块](04-core-modules.md)。

---

## 📌 基础类型

```python
Segments = list[dict[str, Any]]
Event = dict[str, Any]
```

OneBot 消息段结构：

```python
{
    "type": "text",
    "data": {"text": "你好"},
}
```

---

## 📌 `core.plugin_base`

### 消息段构造

| 函数 | 返回消息段 | 用途 |
|---|---|---|
| `text(content)` | `text` | 文本 |
| `image(file_path)` | `image` | 本地图片，路径转换为 `file://` URI |
| `emoji(file_path, summary="")` | `emoji` | 本地表情图片与可选摘要 |
| `image_url(url)` | `image` | 远程图片 URL |
| `face(face_id)` | `face` | QQ face |
| `record(file_path)` | `record` | 本地语音 |
| `record_url(url)` | `record` | 远程语音 URL |
| `segments(payload)` | `Segments` | 字符串、列表或空值标准化 |

示例：

```python
from core.plugin_base import image, segments, text

reply = [
    text("结果如下"),
    image("data/example/result.png"),
]

plain_reply = segments("完成")
```

### Action 构造

```python
build_action(
    segs: Segments,
    user_id: int | None,
    group_id: int | None,
) -> dict[str, Any] | None
```

`group_id` 存在时生成 `send_group_msg`，其余用户作用域生成 `send_private_msg`。
空消息段返回空 Action。

### 文本边界

```python
has_control_characters(
    value: str,
    *,
    allow_formatting_whitespace: bool = False,
    include_c1: bool = False,
) -> bool
```

```python
bounded_external_text(
    value: object,
    *,
    max_chars: int,
    max_bytes: int,
    default: str = "",
    suffix: str = "…",
    strip_ansi: bool = True,
    strip: bool = True,
    truncate: bool = True,
) -> str
```

```python
head_tail_preview(text: str, max_chars: int, *, marker: str) -> str
```

这些函数用于第三方响应、终端输出、标题和日志摘要的字符、字节与控制字符边界。

```python
file_uri_to_path(value: str) -> Path | None
```

此函数将消息段中的 `file://` URI 转换为本地路径。它兼容 `file:///C:/x`、`file://C:/x`、`file://localhost/x` 和 UNC。其他 URI 类型返回 `None`。
`image()`、`record()` 生成 URI 的方向由 Core 处理。

### 外部命令

```python
from core.process_tree import ProcessResult, run_process

result = await run_process(argv, timeout=30, cwd=workdir, max_capture_bytes=64 * 1024)
```

命令在独立进程树里执行（Windows 用 Job 对象，POSIX 用进程组）。
stdout 与 stderr 共用捕获预算，超时、超出预算或调用方取消时整树回收，命令自己退出后留下的后台子进程也会收掉，不会借收尾多跑。
`ProcessResult` 含 `exit_code`（被终止时为 `None`）、`stdout`、`stderr`、`timed_out`、`output_limited`。

### 异步工具

```python
await run_sync(func, *args, **kwargs)
```

`run_sync()` 将同步函数提交到当前插件的执行 bulkhead。

```python
await gather_bounded(awaitables, *, limit: int)
```

按输入顺序返回结果，并限制同时运行的 awaitable 数量。

### 文件工具

```python
ensure_dir(path: Path) -> None
atomic_write_bytes(path: Path, payload: bytes) -> None
atomic_write_text(path: Path, payload: str) -> None
load_json(path: Path, default=None, *, raise_on_error=False)
write_json(path: Path, data: Any) -> None
```

`atomic_write_bytes()` 通过同目录临时文件、`fsync` 和原子替换提交字节。
`atomic_write_text()` 使用 UTF-8 编码调用同一原语。
`load_json()` 与 `write_json()` 使用 `AtomicJsonStore`。
持久数据路径以 `context.data_dir` 为根。

### 长消息拆分

```python
split_message_segments(
    segs: Segments,
    max_length: int = MAX_MESSAGE_TEXT_LENGTH,
) -> list[Segments]
```

函数按文本边界拆分长消息，并保留混合消息段顺序。

---

## 🧩 `PluginContext`

公开结构由 `core.interfaces.PluginContextProtocol` 定义。

### 身份与路径属性

| 属性 | 类型 | 说明 |
|---|---|---|
| `plugin_name` | `str` | 当前插件规范名 |
| `plugin_dir` | `Path` | 当前插件源码根目录 |
| `data_dir` | `Path` | 当前插件持久数据目录 |
| `current_user_id` | `int \| None` | 当前用户 |
| `current_group_id` | `int \| None` | 当前群 |
| `request_id` | `str \| None` | 当前请求追踪 ID |
| `principal` | `PluginPrincipal` | 当前调用身份 |
| `command_invocation` | `CommandInvocation \| None` | Core 解析后的命令节点 |
| `state` | `dict[str, Any]` | 当前插件代共享内存状态 |

### 共享服务属性

| 属性 | 类型 | 说明 |
|---|---|---|
| `logger` | logger adapter | 自动附加 request ID 的插件日志器 |
| `http_session` | `aiohttp.ClientSession \| None` | 应用拥有的共享 HTTP Session |
| `send_action` | async callable | 主动发送 OneBot Action，并返回投递结果 |
| `metrics` | `MetricsCollector \| None` | 运行指标 |
| `capabilities` | `PluginCapabilities` | Manifest 与身份共同签发的能力 |

共享 HTTP Session 由应用生命周期统一管理。
插件在请求作用域内复用该对象。

```python
await context.send_action(action: dict[str, Any]) -> bool | None
```

返回 `True` 表示拆分后的全部 Action 已确认交给投递边界，`False` 表示至少一个 Action 已确认失败，`None` 表示传输端可能已经提交 Action 且最终结果未知。
结果未知时由业务层保留待确认状态，避免自动重试造成重复消息。

携带登录码、Token 等凭据的消息在发送前设置敏感标记：

```python
from core.delivery import SENSITIVE_ACTION_KEY

action = build_action(segments(message), user_id, None)
action[SENSITIVE_ACTION_KEY] = True
await context.send_action(action)
```

带标记的 Action 不会交给 `core.observe_outgoing_action` 观察者（例如聊天插件的上下文记录），投递层在序列化前移除该标记和内部收据字段。
Pendo 的 Web 登录码与 Widget Token 使用该标记。

### 配置方法

```python
context.get_config(path: str) -> Any
context.get_secret(path: str) -> Any
context.get_settings_snapshot() -> PluginSettingsSnapshot
context.now() -> datetime
context.default_groups() -> list[int]
```

`get_config()` 与 `get_secret()` 接受当前插件命名空间内的点路径。
`get_settings_snapshot()` 返回同一 revision 的只读配置与 secret 视图。

### `PluginSettingsSnapshot`

| 属性或方法 | 类型 | 说明 |
|---|---|---|
| `config` | `Mapping[str, Any]` | 当前 revision 的公开配置快照 |
| `secrets` | `Mapping[str, Any]` | 当前 revision 的敏感配置快照 |
| `revision` | `int` | 原子发布版本号 |
| `config_status` | `str` | 公开配置来源状态 |
| `secrets_status` | `str` | 敏感配置来源状态 |
| `plugin_config(plugin_name)` | `Mapping[str, Any]` | 指定插件的公开命名空间 |
| `plugin_secrets(plugin_name)` | `Mapping[str, Any]` | 指定插件的敏感命名空间 |

Context 返回的快照已限定到当前插件可见范围。
插件在一次业务事务中复用同一对象，可以保证公开配置与凭据来自同一 revision。

### `PluginPrincipal`

| 属性 | 类型 | 说明 |
|---|---|---|
| `kind` | `Literal["user", "scheduled_system", "lifecycle"]` | 调用主体类型 |
| `user_id` | `int \| None` | 用户主体的 QQ 号 |
| `group_id` | `int \| None` | 用户主体所在群 |
| `is_bot_admin` | `bool` | 签发时的 Bot 管理员状态 |
| `is_private` | `bool` | 用户主体来自私聊 |
| `group_role` | `Literal["owner", "admin", "member", "unknown"]` | 当前群角色 |
| `delivery_targets` | `tuple[DeliveryTarget, ...]` | Core 校验后的主动投递目标 |
| `schedule_delivery` | `ScheduleDeliveryMode \| None` | 调度主体的 Core 投递模式 |

`DeliveryTarget.kind` 为 `private` 或 `group`，`target_id` 为正整数。
`principal.is_system` 标识调度主体，`principal.can_manage_group(group_id)` 校验当前用户是否可管理指定群。

### 静音方法

```python
context.mute_group(group_id: int, duration_minutes: float) -> None
context.unmute_group(group_id: int) -> bool
context.is_group_muted(group_id: int) -> bool
context.get_mute_remaining(group_id: int) -> float
```

`get_mute_remaining()` 返回剩余静音时间（分钟）。

### 应用操作

```python
context.reload_config() -> None
context.reload_plugins() -> asyncio.Task[None] | None
context.get_command_catalog() -> tuple[CommandCatalogNode, ...]
context.list_plugins() -> list[str]
```

这些方法属于受信任插件的进程内 API，已加载插件可直接调用。
Bot Core 的用户命令按 Manifest 声明，由 Dispatcher 在调用前用 `command_access` 校验 Bot 管理员与私聊场景。
部署者通过插件来源审查保护代码调用边界。

---

## 🧩 Capability

`context.capabilities` 包含 Core 签发的窄能力：

| 属性 | 用途 |
|---|---|
| `is_bot_admin` | 当前 principal 的 Bot 管理员状态。只用于对象层面的规则，命令权限由 Manifest 声明、Core 判断 |
| `is_system` | 调度调用状态 |
| `secret_admin` | Bot Core 的敏感配置管理（跟随 `/set_secret` 的声明，每次调用复核） |
| `onebot_media` | XiaoQing Chat 的 OneBot 媒体查询 |
| `config_subscription` | Pendo 的配置 revision 订阅 |
| `codex_arxiv_summary` | arXiv Filter 到 Codex 摘要服务（定时任务可用。用户触发时跟随 `/codex` 的声明） |
| `voice_synthesis` | Smalltalk 到 Voice 合成服务 |
| `chat_reply` | Smalltalk 到 Chat 回复服务 |
| `agent_turn` | XiaoQing Chat 到 XiaoQing Agent 的一轮交接（用户消息。可分判断与执行两步），并回报当轮回复是否发出 |
| `codex_agent` | XiaoQing Agent 到 Codex 的同步任务（跟随 `/codex` 的声明，每次调用复核） |
| `ssh_agent` | XiaoQing Agent 到 QingSSH 的服务器列表与一次性命令（跟随 `/ssh` 的声明，每次调用复核） |
| `command_runner` | XiaoQing Agent 以发起人身份执行已发布命令，权限与场景检查同本人直接发送。`run(text, *, sender_name, operation_id)` 原样返回命令的 `DeliverySegments`，由调用方结算回执 |
| `local_tools` | XiaoQing Agent 在本机执行代码、读写文件前的授权确认（跟随 `/shell` 的声明，每次调用复核） |
| `ai` | 当前插件的统一 AI route |

`is_bot_admin` 与 `is_system` 始终提供布尔值。
应用签发的 Context 始终提供 `ai` 服务，具体调用由当前插件的 route、provider、model 和凭据配置决定。
其余可选服务根据 Manifest 的 `capabilities`、`uses_services`、服务绑定和当前 principal 签发，条件未满足时属性值为 `None`。
协议将服务属性声明为可选值，便于最小 Context 实现和测试替身表达不可用状态。

### AI Capability

```python
await context.capabilities.ai.complete(
    route: str,
    messages: list[dict[str, Any]],
    *,
    required_modalities: tuple[str, ...] = ("text",),
    pinned_model: str | None = None,
    temperature: float | None = None,
    top_p: float | None = None,
    max_tokens: int | None = None,
    timeout_seconds: float | None = None,
    total_timeout_seconds: float | None = None,
    max_retry: int | None = None,
    retry_interval_seconds: float | None = None,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: Any = None,
    extra_payload: Mapping[str, Any] | None = None,
) -> AICompletionResult
```

```python
context.capabilities.ai.list_models(
    route: str,
    *,
    required_modalities: tuple[str, ...] = ("text",),
) -> tuple[AIModelInfo, ...]
```

### `AIModelInfo`

| 属性 | 类型 | 说明 |
|---|---|---|
| `name` | `str` | 项目配置中的模型 profile 名 |
| `provider` | `str` | Provider 名 |
| `model` | `str` | 发送给 Provider 的模型名 |
| `modalities` | `tuple[str, ...]` | 模型声明的输入模态 |

### `AICompletionResult`

| 属性 | 类型 | 说明 |
|---|---|---|
| `response` | `dict[str, Any]` | Provider 返回的规范响应 |
| `profile` | `str` | 最终成功的模型 profile |
| `provider` | `str` | 最终成功的 Provider |
| `model` | `str` | 最终成功的模型名 |
| `finish_reason` | `str` | 补全结束原因 |
| `attempts` | `int` | 当前模型链的请求尝试总数 |
| `content` | `str` | 常见 Chat Completions 文本内容 |

复杂响应可读取 `response`。
纯文本调用优先读取 `content`。

---

## ⌨️ 命令目录类型

### `CommandCatalogNode`

命令目录由 Core 从已验证 Manifest 生成，并以不可变快照提供给帮助、导出、权限校验和插件调用。

| 属性 | 类型 | 说明 |
|---|---|---|
| `code` | `str` | 稳定命令码 |
| `plugin` | `str` | 所属插件规范名 |
| `path` | `tuple[str, ...]` | 从根命令到当前节点的规范路径 |
| `name` | `str` | 当前节点规范名 |
| `aliases` | `tuple[str, ...]` | 当前节点别名 |
| `help_text` | `str` | 简短帮助 |
| `usage` | `str` | 完整用法 |
| `match_mode` | `str` | `prefix` 或 `exact` |
| `permission` | `str` | `public`、`bot_admin` 或 `group_admin` |
| `contexts` | `tuple[str, ...]` | `private`、`group` 场景集合 |
| `examples` | `tuple[str, ...]` | 正确样例 |
| `invalid_examples` | `tuple[str, ...]` | 错误样例 |
| `children` | `tuple[CommandCatalogNode, ...]` | 直接子命令 |

```python
node.walk() -> tuple[CommandCatalogNode, ...]
node.resolve_child(token: str) -> CommandCatalogNode | None
node.to_dict() -> dict[str, Any]
```

`walk()` 按目录顺序返回自身与全部后代。
`resolve_child()` 按规范名或别名解析直接子节点。
`to_dict()` 返回可直接 JSON 序列化的公开目录。

### `CommandInvocation`

| 属性或方法 | 类型 | 说明 |
|---|---|---|
| `root` | `CommandCatalogNode` | 顶层命令节点 |
| `chain` | `tuple[CommandCatalogNode, ...]` | 实际匹配的节点链 |
| `remainders` | `tuple[str, ...]` | 每个路径深度消费后的原始余串 |
| `node` | `CommandCatalogNode` | 最深匹配节点 |
| `arguments` | `str` | 最深节点后的业务参数 |
| `remainder_after(depth)` | `str` | 指定路径深度后的余串，根节点深度为 0 |

### `command_access()`

```python
from core.router import command_access

command_access(node, principal, *, is_bot_admin=None) -> str | None
```

全项目唯一的命令权限判断：按节点的 `permission` 与 `contexts` 判断此人能否在当前私聊或群里使用。
能用时返回 `None`，否则返回 `"bot_admin"`、`"group_admin"`（没有权限）或 `"context"`（有权限但当前场景不能用）。
先判断权限再判断场景。
`is_bot_admin` 给出时代替签发时的快照，供长任务按当前配置复核。
命令执行、会话继续、Core 签发的派生能力和 agent 的命令清单都调用它。

---

## 📌 发送回执（`core.delivery`）

| 接口 | 说明 |
|---|---|
| `DeliveryReceipt(expected_actions, commit, rollback, unknown)` | 一个逻辑回复的回执。全部物理消息确认后调用 `commit`，明确失败调用 `rollback`，结果未知调用 `unknown`，至多结算一次 |
| `receipt.record(delivered)` | 记录一次传输结果：`True`、`False` 或 `None`（未知） |
| `receipt.message_ids` | 已确认发出的各条物理消息 ID，按发送顺序。长回复被拆成几条时每条都有，提交回调里读取 |
| `receipt.resolved` / `committed` / `outcome` | 是否已结算、提交回调是否成功、最终三态结果 |
| `DeliverySegments(values, receipt)` | 带回执的消息段。插件入口返回它，Core 随实际投递结算 |
| `send_with_receipt(send_action, action, receipt)` | 主动发送一条带回执的 Action，并确保结果恰好结算一次 |

---

## 🌐 Session API

### Context 方法

```python
await context.create_session(
    initial_data: dict[str, Any] | None = None,
    timeout: float | None = None,
) -> Session
```

```python
await context.get_session() -> Session | None
await context.update_session(callback: Callable[[Session], Any]) -> Any
await context.end_session() -> bool
await context.has_session() -> bool
```

`get_session()` 返回隔离快照，并刷新空闲超时。
`update_session()` 为 callback 提供私有工作副本。
成功返回会提交副本，异常与取消会回滚该次事务。
`has_session()` 查询活动状态。

Session 到期后结束。
后续插件可再次调用 `create_session()` 建立新会话。

### `Session` 属性

| 属性 | 类型 | 说明 |
|---|---|---|
| `user_id` | `int` | 用户 ID |
| `group_id` | `int \| None` | 群 ID |
| `plugin_name` | `str` | 会话插件 |
| `session_id` | `str` | 唯一会话 ID |
| `state` | `str` | 业务状态 |
| `data` | `dict[str, Any]` | 业务数据 |
| `created_at` | `float` | 创建时间戳 |
| `updated_at` | `float` | 更新时间戳 |
| `timeout` | `float` | 空闲超时秒数 |
| `version` | `int` | 本地版本计数 |

### `Session` 方法

```python
session.get(key, default=None) -> Any
session.set(key, value) -> None
session.delete(key) -> bool
session.clear() -> None
session.update() -> None
session.is_expired() -> bool
```

持久修改在 `context.update_session(callback)` 的工作副本中执行。

---

## 📌 参数解析

```python
from core.args import parse, parse_int, tokenize
```

### `parse()`

```python
args = parse('alpha "two words" --limit 5 -v')

args.tokens  # ["alpha", "two words"]
args.first  # "alpha"
args.second  # "two words"
args.get(0)  # "alpha"
args.rest(1)  # "two words"
args.opt("limit")  # "5"
args.has("v")  # True
```

支持单字母短选项、ASCII 长选项、`--key=value` 和 `--` 选项终止符。

### `parse_int()`

```python
parse_int(text, *, minimum=None, maximum=None) -> int | None
```

函数接受 ASCII 十进制整数字符串，并应用可选范围。

---

## ⌨️ 插件入口签名

以下示例采用推荐的异步定义。
Core 同时接受相同参数签名的同步 `def` 回调。
同步回调进入当前插件的同步 bulkhead，回调返回 awaitable 时 Core 继续等待其完成。

### 命令

```python
async def handle(
    command: str,
    args: str,
    event: Event,
    context: PluginContextProtocol,
) -> Segments | list[dict[str, Any]] | str | None: ...
```

各入口返回的消息段都可以是带回执的 `DeliverySegments`。

### 会话

```python
async def handle_session(
    text: str,
    event: Event,
    context: PluginContextProtocol,
    session: Session,
) -> Segments | list[dict[str, Any]] | str | None: ...
```

### 闲聊

```python
async def handle_smalltalk(
    text: str,
    event: Event,
    context: PluginContextProtocol,
) -> Segments | list[dict[str, Any]] | str | None: ...
```

### URL

```python
async def handle_url(
    url: str,
    event: Event,
    context: PluginContextProtocol,
) -> Segments | list[dict[str, Any]] | str | None: ...
```

### 生命周期

```python
async def init(context: PluginContextProtocol) -> None: ...


async def shutdown(context: PluginContextProtocol) -> None: ...
```

### 调度

```python
async def scheduled_handler(
    context: PluginContextProtocol,
) -> Segments | ScheduledDelivery | list[ScheduledDelivery] | str | None: ...
```

返回类型由 Manifest 的 `delivery` 决定：`broadcast` 使用普通消息段或字符串，`targeted` 使用一个或多个 `ScheduledDelivery`，`silent` 返回 `None`。
Core 负责目标校验、OneBot Action 构建与最终发送。

`targeted` 处理器通过 Core 类型构造目标消息：

```python
ScheduledDelivery.group(group_id, message_segments, receipt=None)
ScheduledDelivery.private(user_id, message_segments, receipt=None)
```

群目标必须属于当前 schedule 经 Core 解析后的 `group_ids`。
私聊目标必须是正整数 QQ 号。
`receipt` 可选，用于在 OneBot 投递结果确定后提交业务状态。

---

## 🌐 OneBot 事件字段

插件常用字段：

```python
event.get("post_type")
event.get("message_type")
event.get("user_id")
event.get("group_id")
event.get("message_id")
event.get("message")
event.get("raw_message")
event.get("sender")
```

`event["message"]` 在 Core 入口完成消息段标准化。
插件可结合 `core.message` 工具提取文本和媒体段。
`sender_display_name(event["sender"])` 取发送者显示名（群名片、昵称、名称依次取第一个非空的）。

---

## 🌐 Inbound API

### `POST /event`

- 请求体：OneBot v11 事件 JSON 对象，媒体类型为 `application/json` 或 `application/*+json`
- 鉴权：`Authorization: Bearer <inbound_token>`
- 标准成功响应：`{}`；应用通过 OneBot action API 投递回复并等待确认
- 自定义动作列表响应：显式发送 `X-XiaoQing-Response-Mode: actions`，收到 `{"actions": [...]}`；调用者负责执行动作，该模式用于命令矩阵
- 主要状态：`200` 成功，`400` JSON 或事件结构错误，`401` 鉴权失败，`415` 媒体类型错误，`500` 处理器异常，`503` 服务关闭或接纳队列过载

### `WebSocket /ws`

- 入站：OneBot v11 事件 JSON
- 出站：OneBot Action JSON
- 鉴权：与 HTTP Inbound 共享 token 策略

### `GET /health`

返回服务状态、运行时版本、在线时长、请求数和 WebSocket 连接数，并按可用性附加插件、Session 与任务计数。

### `GET /metrics`

返回消息、插件、错误、延迟和队列指标。
指标提供器未配置时返回 `501`，指标生成失败时返回 `500`。

`/health`、`/metrics`、`POST /event` 与 WebSocket 握手共享同一 Bearer token。
Listener 启用时使用非空 `inbound_token`。

---

## 🌐 OneBot Action

群消息：

```python
{
    "action": "send_group_msg",
    "params": {
        "group_id": 123456789,
        "message": [{"type": "text", "data": {"text": "你好"}}],
    },
}
```

私聊消息：

```python
{
    "action": "send_private_msg",
    "params": {
        "user_id": 123456789,
        "message": [{"type": "text", "data": {"text": "你好"}}],
    },
}
```

---

## 🧭 下一步

- Manifest 与开发流程：[插件开发指南](03-plugin-development.md)
- 配置字段：[配置详解](06-configuration.md)
- 运行模式：[高级主题](07-advanced.md)
