# 🧩 03 - 插件开发指南

本章说明 XiaoQing 插件的目录结构、Manifest、运行入口、Context、会话、调度、服务能力和验证流程。接口签名速查见 [API 参考](05-api-reference.md)。

---

## 🔐 插件信任模型

插件是与 Bot 同进程、同操作系统权限运行的受信任 Python 扩展。部署者负责插件来源与代码审查。Core 为插件提供作用域配置、secret、数据目录、capability、执行预算和生命周期管理。

插件执行 gate 负责并发、排队、超时、熔断和代际排空。安全部署采用可信插件集合、最小系统权限、回环网络与受控反向代理。

---

## 🚀 五步创建插件

以下示例创建 `hello` 插件。

### 1. 创建目录

```text
plugins/hello/
├── __init__.py
├── plugin.json
└── main.py
```

目录名与 Manifest `name` 保持一致，并使用小写 ASCII Python 标识符。

### 2. 编写 Manifest

```json
{
  "schema_version": 1,
  "name": "hello",
  "version": "0.1.0",
  "description": "问候示例",
  "entry": "main.py",
  "concurrency": "parallel",
  "commands": [
    {
      "name": "hello",
      "triggers": ["hello", "你好"],
      "help": "向指定名字问好",
      "usage": "/hello <名字>",
      "contexts": ["private", "group"],
      "examples": ["/hello 小青"],
      "invalid_examples": ["/hello"]
    }
  ],
  "schedule": [],
  "dependencies": []
}
```

### 3. 编写入口

```python
from __future__ import annotations

from typing import Any

from core.interfaces import PluginContextProtocol
from core.plugin_base import segments


async def handle(
    command: str,
    args: str,
    event: dict[str, Any],
    context: PluginContextProtocol,
) -> list[dict[str, Any]]:
    name = args.strip()
    if command == "hello" and name:
        return segments(f"你好，{name}！")
    return segments("用法：/hello <名字>")
```

插件子模块使用相对导入：

```python
from .formatters import format_greeting
```

### 4. 加载插件

启动项目：

```bash
python main.py
```

运行中的管理员可发送 `/reload` 重新扫描插件。启用 `enable_plugin_watcher` 后，文件快照变化会触发热重载或 restart-only 提示。

### 5. 验证插件

```text
/plugins
/help hello
/hello 小青
```

提交前执行：

```bash
python -m compileall -q plugins/hello
python -m pytest -q
python -m ruff check plugins/hello
python scripts/format_code.py --check plugins/hello
git diff --check
```

提交前使用 `python scripts/format_code.py --check` 检查全仓格式。新增插件沿用局部赋值对齐、紧凑表达与关键业务边界中文注释。

---

## 代码与验证约定

项目支持 Python 3.11 及以上版本。Python 代码采用局部赋值对齐、100 列内的紧凑表达和解释业务边界的中文注释。公共 API、数据格式和异常语义由实现与回归测试共同约束。

### 排版

使用 `python scripts/format_code.py` 格式化全部受版本控制及新建的代码文件，使用 `python scripts/format_code.py --check` 执行只读检查。也可在命令后指定文件或目录。Python 格式器先调用 Ruff 收敛表达式布局，再对齐同缩进、相邻逻辑块中的赋值与多行关键字参数；转换前后校验 AST 一致性。JavaScript 与 PowerShell 通过 Pygments 定位声明赋值，并验证转换前后的非空白 token 一致。HTML、CSS、Shell 与 VBS 纳入文件清单并保留原有语法布局。

```python
owner_id    = event.user_id
timeout     = settings.timeout
retry_limit = settings.retry_limit
```

独立逻辑块用空行分开。条件、调用和容器能在行宽内清晰表达时保持紧凑；复杂表达式保留分行。字符串和注释内容由词法边界保护。JavaScript、HTML 和 PowerShell 保留各语言语法与现有缩进，局部对齐只用于相邻且含义相关的声明。

### 去重与注释

相同状态归属、失败语义和生命周期的实现可以抽取到公共工具。不同协议、资源所有者和事务边界保持明确。删除不可达分支、失效兼容代码与无用导入时，同时核对调用者、清单入口、动态加载和测试引用。

中文注释说明时间与金额单位、取消期间的资源所有权、原子提交顺序、缓存失效、权限边界和兼容约定。函数名已经清晰表达的简单动作可保持简洁。测试注释说明触发条件、预期契约和隔离方式。

### 验证与文档

```bash
python scripts/format_code.py --check
python -m ruff check .
python -m mypy core plugins
python -m pytest -q -n auto --cov=core --cov=plugins --cov-report=term-missing
git diff --check
```

先运行受影响模块和新增回归，再执行全仓库门禁。格式操作校验语法树保持一致，业务修复通过可触发旧问题的行为测试验证。测试数据使用临时目录或临时数据库。

README 描述当前用法和配置；ARCHITECTURE 描述模块职责、数据流与所有权；项目手册描述公共契约。命令、权限和别名与 `plugin.json` 保持一致，跨模块公共约定通过链接引用。审查记录分别维护问题处置、修复批次和最终验证，全部完成后再标记关闭。

---

## 💾 插件目录与发行资源

常见目录：

```text
plugins/example/
├── __init__.py
├── plugin.json
├── main.py
├── handlers/
│   ├── __init__.py
│   └── commands.py
├── resources/
│   └── template.json
└── README.md
```

Core 为每个加载代冻结以下输入：

- 全部 Python 源码
- 插件根目录 JSON
- `watch_files` 声明的嵌套文件

运行数据写入 `context.data_dir`。发行资源通过 setuptools package-data 进入 wheel/sdist，并由资源契约测试校验。

入口路径使用插件目录内的规范 POSIX `.py` 相对路径，例如 `main.py` 或 `handlers/main.py`。源码与资源路径保持在插件真实目录内。

---

## 🧩 Manifest 参考

### 顶层字段

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `schema_version` | `1` | `1` | Manifest schema 版本 |
| `name` | string | 必填 | 插件规范名，与目录名一致 |
| `version` | string | `0.0.0` | 插件版本 |
| `description` | string | 空 | 用户可见功能摘要 |
| `author` | string | 空 | 作者信息 |
| `entry` | string | `main.py` | Python 入口相对路径 |
| `enabled` | boolean | `true` | 启动加载开关 |
| `concurrency` | `parallel` / `sequential` | `parallel` | 插件入口并发模式 |
| `commands` | array | `[]` | 递归命令目录 |
| `schedule` | array | `[]` | cron 任务 |
| `dependencies` | array | `[]` | Python 导入依赖声明 |
| `watch_files` | array | `[]` | 额外快照文件 |
| `services` | array | `[]` | 声明式服务导出 |
| `uses_services` | array | `[]` | 声明式服务消费 |
| `capabilities` | array | `[]` | Core 维护的窄能力请求 |

Core 在导入插件前检查 `dependencies` 中的模块可用性。部署环境通过 `requirements.txt`、项目 extra 或自身包管理流程安装依赖。

依赖对象格式：

```json
{
  "name": "PIL",
  "required": true,
  "description": "图片处理"
}
```

`required: true` 的模块决定插件加载条件。`required: false` 适合保留帮助或降级功能的可选模块。

依赖字段：

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `name` | string | 必填 | 可由 `importlib.util.find_spec()` 定位的 Python 模块名 |
| `required` | boolean | `true` | 模块缺失时控制插件加载结果 |
| `description` | string / `null` | `null` | 依赖用途说明 |

`dependencies` 描述 Python 导入前置条件。插件之间的协作使用 `services` 与 `uses_services`，加载事务会统一核验所有者、调用方和服务可用性。

### 命令字段

顶层命令对象：

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `name` | string | 必填 | 稳定命令名，用于命令 code |
| `triggers` | string[] | 必填 | 非空且唯一的顶层触发词；匹配区分大小写 |
| `help` | string | 必填 | 功能摘要 |
| `usage` | string | `/<首个 trigger>` | 完整用法 |
| `admin_only` | boolean | `false` | 顶层 Bot 管理员快捷声明；`true` 归一化为 `bot_admin` |
| `permission` | `public` / `bot_admin` / `group_admin` | `public` | 权限级别 |
| `contexts` | `private` / `group` 数组 | 两者 | 使用场景 |
| `priority` | integer | `0` | 顶层路由优先级，数值较大者先匹配 |
| `examples` | string[] | `[]` | 正确样例，最多 16 条 |
| `invalid_examples` | string[] | `[]` | 参数、场景或权限错误样例，最多 16 条 |
| `subcommands` | array | `[]` | 子命令节点 |

子命令节点：

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `name` | string | 必填 | 同级唯一的规范名 |
| `aliases` | string[] | `[]` | 同级唯一的别名 |
| `help` | string | 必填 | 功能摘要 |
| `usage` | string | 必填 | 完整用法 |
| `match` | `prefix` / `exact` | `prefix` | `prefix` 保留后续业务参数；`exact` 只接受完整节点路径 |
| `permission` | 权限枚举 | 继承父节点 | 子节点可收紧权限 |
| `contexts` | 场景数组 | 继承父节点 | 子节点可收窄场景 |
| `examples` | string[] | `[]` | 正确样例 |
| `invalid_examples` | string[] | `[]` | 错误样例 |
| `subcommands` | array | `[]` | 下一层节点 |

整棵命令目录最多 8 层、512 个节点。Core 将父子权限合成为较严格级别，并取父子场景交集。`group_admin` 接受当前群群主、当前群管理员和 Bot 管理员；`bot_admin` 接受 Bot 管理员。

Router 依次比较 `priority` 和触发词长度。两个插件声明相同触发词与相同优先级时，加载事务报 `CommandConflictError` 并保留已发布插件代。

```json
{
  "name": "todo",
  "triggers": ["todo", "待办"],
  "help": "管理待办",
  "usage": "/todo <subcommand>",
  "examples": ["/todo list"],
  "invalid_examples": ["/todo mystery"],
  "subcommands": [
    {
      "name": "add",
      "aliases": ["新增"],
      "help": "添加待办",
      "usage": "/todo add <内容>",
      "examples": ["/todo add 写周报"],
      "invalid_examples": ["/todo add"]
    }
  ]
}
```

Core 为上述叶节点生成稳定 code `example.todo.add`。插件通过 `context.command_invocation` 获取已解析节点和剩余参数。

### 调度字段

```json
{
  "handler": "scheduled_daily",
  "id": "hello.daily",
  "cron": {"hour": 8, "minute": 0},
  "delivery": "broadcast",
  "group_ids": [123456789],
  "description": "每日问候",
  "enabled": true
}
```

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `handler` | string | 必填 | 入口模块中的公开异步函数名 |
| `cron` | object | 必填 | APScheduler `CronTrigger` 参数 |
| `id` | string / `null` | `null` | 全局任务 ID；省略时由 Core 生成稳定 ID |
| `delivery` | `broadcast` / `targeted` / `silent` | `broadcast` | Core 执行的投递模式 |
| `group_ids` | positive integer[] / `null` | `null` | 投递群；省略时使用 `default_group_ids` |
| `description` | string / `null` | `null` | 任务说明 |
| `enabled` | boolean | `true` | 任务启用状态 |

Scheduler 在插件发布时校验 handler、cron、任务 ID、投递模式和目标。任务到期时，Core 按以下模式处理：

- `broadcast`：把 handler 返回的消息段投递给该 schedule 的全部目标群；
- `targeted`：接收 `ScheduledDelivery` 目标消息，校验群目标属于该 schedule 的群列表，并支持受控私聊目标；
- `silent`：执行 handler 并保持零投递目标，忽略返回载荷，同时拒绝该调度上下文的主动发送。

`broadcast` 与 `targeted` 使用条目中的 `group_ids`；字段省略或为 `null` 时使用全局 `default_group_ids`。`silent` 不声明 `group_ids`。插件重载会替换该插件拥有的调度任务。

---

## ⌨️ 运行入口

### `handle()`

命令路由命中后调用：

```python
async def handle(command, args, event, context): ...
```

- `command`：Manifest 顶层稳定命令名。
- `args`：顶层触发词之后的文本。
- `event`：原始 OneBot 事件。
- `context`：当前插件、用户、群和 request ID 作用域的 Context。

复合命令优先读取解析结果：

```python
invocation = context.command_invocation
node_name = invocation.node.name
arguments = invocation.arguments
```

#### 参数解析

简单位置参数可直接处理 `args`。带引号、选项或整数边界的命令使用 Core 解析器：

```python
from core.args import parse, parse_int, tokenize

parsed = parse(args)
target = parsed.first
limit = parse_int(parsed.opt("limit"), minimum=1, maximum=100)
verbose = parsed.has("v")
words = tokenize(args, strict=True)
```

`parse()` 支持单字母短选项、ASCII 长选项、`--key=value` 和 `--` 选项终止符。`tokenize()` 默认把未闭合引号按空白切分；命令协议需要完整引号时使用 `strict=True`。`parse_int()` 接受 ASCII 十进制整数，并返回经过范围校验的 `int` 或 `None`。完整返回结构见 [API 参考的参数解析](05-api-reference.md#参数解析)。

### `handle_session()`

当前会话键存在活动 Session 时调用：

```python
async def handle_session(text, event, context, session): ...
```

处理函数读取传入快照，并通过 `context.update_session()` 提交持久修改。

### `handle_smalltalk()`

插件作为 `smalltalk_provider` 时调用：

```python
async def handle_smalltalk(text, event, context): ...
```

### `handle_url()`

`url_parser` 类插件可提供：

```python
async def handle_url(url, event, context): ...
```

### `call_bot_name_only()`

当前 `smalltalk_provider` 可处理只包含 Bot 名称的消息：

```python
async def call_bot_name_only(context):
    return segments("我在")
```

### 入口选择顺序

完成消息清洗、群聊门控和静音判断后，Dispatcher 按消息形态选择入口：

1. 纯 URL 调用 `handle_url()`。
2. 只包含 Bot 名称时调用 `call_bot_name_only()`。
3. 活动会话调用所属插件的 `handle_session()`；返回 `None` 时继续命令路由。
4. 命令目录命中后调用 `handle()`。
5. 普通文本回落到当前 `smalltalk_provider` 的 `handle_smalltalk()`。

带命令前缀的未知命令由 Core 返回统一提示。会话、命令和 URL 输入受观察边界保护，闲聊 provider 只观察符合消息策略的普通对话。

### 生命周期

```python
async def init(context): ...


async def shutdown(context): ...
```

`init()` 创建插件所有资源。`shutdown()` 按创建顺序的逆序关闭任务、连接、数据库和 Web 服务。插件代际发布会在初始化成功后提交入口。

### 调度函数

```python
async def scheduled_daily(context):
    return segments("早上好")
```

`broadcast` 调度 Context 使用 system principal 和声明的发送目标，返回消息段由 Scheduler 投递到全部目标群。各群内容需要独立生成时使用 `targeted`：

```python
from core.delivery import ScheduledDelivery


async def scheduled_group_status(context):
    return [
        ScheduledDelivery.group(group_id, segments(f"群 {group_id} 状态正常"))
        for group_id in context.default_groups()
    ]
```

纯维护任务使用 `silent`，handler 完成数据清理、缓存刷新或资源回收后返回 `None`。

---

## 🧩 PluginContext

### 身份与路径

```python
context.plugin_name
context.plugin_dir
context.data_dir
context.current_user_id
context.current_group_id
context.principal
context.request_id
```

### 配置与 secret

单项读取：

```python
model = context.get_config("ai.default_model")
api_key = context.get_secret("provider.api_key")
```

同一操作需要多项配置时，读取一个原子快照：

```python
settings = context.get_settings_snapshot()
config = settings.plugin_config(context.plugin_name)
secrets = settings.plugin_secrets(context.plugin_name)
revision = settings.revision
```

`settings.config` 包含安全共享字段与当前插件公开命名空间，`settings.secrets` 限定在当前插件命名空间，其中只保留敏感业务映射。`plugin_config()` 与 `plugin_secrets()` 直接取得业务映射。Core capability 承担管理员 secret、OneBot 媒体和配置订阅等特权操作。

时间与当前调用的有效投递目标来自同一 Core 上下文：

```python
now = context.now()
group_ids = context.default_groups()
```

在 `broadcast` 与 `targeted` 定时任务中，`context.default_groups()` 返回该 schedule 经 Core 解析后的目标群；`silent` 定时任务返回空列表。命令与生命周期调用返回全局 `default_group_ids`。

### 日志与指标

```python
context.logger.info("处理完成")
```

插件日志器自动附加 request ID。日志字段使用有界标识、计数、状态和耗时；token、Cookie、授权头与用户敏感正文使用摘要或脱敏值。Core 已对插件入口的调用次数、耗时、错误和取消进行统一计量。

### 插件代内状态

`context.state` 是当前插件代共享的内存字典，适合缓存和任务注册表。持久状态写入 `context.data_dir`。

### 管理员与静音

```python
context.is_global_admin()
context.mute_group(group_id, duration_minutes=30)
context.unmute_group(group_id)
context.is_group_muted(group_id)
context.get_mute_remaining(group_id)
```

`get_mute_remaining()` 返回分钟。

### 应用服务与调用身份

```python
catalog = context.get_command_catalog()
loaded_plugins = context.list_plugins()
is_admin = context.capabilities.is_bot_admin
is_scheduled = context.capabilities.is_system
```

`context.principal` 保存用户、群、会话类型和投递目标。`context.capabilities` 保存 Core 按插件声明与当前身份签发的窄能力。主动 OneBot Action 使用 `await context.send_action(action)`；管理型插件可通过自身命令权限调用 `context.reload_config()` 或 `context.reload_plugins()`。完整签名与 capability 属性见 [API 参考](05-api-reference.md#plugincontext)。

---

## 💬 会话

创建会话：

```python
await context.create_session({"step": "name"}, timeout=300.0)
```

读取快照：

```python
session = await context.get_session()
```

原子更新：

```python
async def update(working):
    working.set("step", "confirm")
    working.set("value", "小青")


await context.update_session(update)
```

结束与查询：

```python
await context.end_session()
active = await context.has_session()
```

同一会话键串行执行。群聊键由群 ID 与用户 ID 组成，私聊键由用户 ID 组成。Session 超时后结束；后续业务可再次调用 `create_session()` 开始新流程。

---

## 💬 消息返回

使用 `core.plugin_base` 构造消息段：

```python
from pathlib import Path

from core.plugin_base import image, image_url, record, segments, text

reply = [
    text("结果如下"),
    image_url("https://example.com/result.png"),
]

local_image = image(str(Path("result.png").resolve()))
voice = record(str(Path("voice.mp3").resolve()))
plain = segments("完成")
```

入口返回值支持：

- 字符串
- OneBot 消息段列表
- Action 列表
- `None`，表示该调用不返回消息

长文本使用 `split_message_segments()` 按消息段边界拆分。出站文件与远程媒体遵循 Core 的类型、大小、路径和网络预算。

---

## 🌐 HTTP、AI 与同步库

### HTTP

固定上游地址复用 `context.http_session`，并为响应字节、解压比例、MIME、JSON 结构和总时间设置预算：

```python
import aiohttp

from core.bounded_http import (
    BodyLimits,
    JsonLimits,
    MimePolicy,
    aiohttp_request_bounded,
    parse_bounded_json,
)

session = context.http_session
if session is None:
    raise RuntimeError("共享 HTTP Session 尚未就绪")

response = await aiohttp_request_bounded(
    session,
    "GET",
    "https://api.example.com/v1/status",
    limits=BodyLimits(max_wire_bytes=512_000, max_decoded_bytes=1_000_000),
    mime_policy=MimePolicy(exact=frozenset({"application/json"})),
    request_kwargs={"timeout": aiohttp.ClientTimeout(total=10)},
)
payload = parse_bounded_json(response, limits=JsonLimits(max_bytes=1_000_000))
```

应用拥有并关闭共享 Session。插件负责每次请求的超时、重定向、响应大小、内容类型和解析结构预算。

聊天内容控制请求目标时使用 `core.safe_http.fetch_public_html()` 或 `fetch_public_bytes()`：

```python
from core.safe_http import fetch_public_html

response = await fetch_public_html(url, timeout_seconds=10)
```

安全 HTTP 客户端逐跳校验 scheme、DNS、目标网段和重定向，并固定已验证地址；返回正文受压缩与解压预算约束。

### 外部文本与图片

第三方服务、RCON、SSH 和网页内容进入消息前，通过共享边界完成字符与媒体校验：

```python
from core.image_validation import ImageValidationLimits, validate_image_bytes
from core.plugin_base import bounded_external_text

safe_text = bounded_external_text(
    payload["title"],
    max_chars=500,
    max_bytes=2_000,
)
image_info = validate_image_bytes(
    image_payload,
    limits=ImageValidationLimits(
        max_bytes=5 * 1024 * 1024,
        max_pixels=20_000_000,
    ),
)
```

`bounded_external_text()` 统一处理 ANSI、控制字符、字符数和 UTF-8 字节数。`validate_image_bytes()` 与 `validate_image_path()` 校验真实格式、容器、尺寸、像素、帧数和文件身份。图片解码等同步工作通过 `run_sync()` 进入插件执行预算。

### AI route

在 `config.plugins.<plugin_name>.ai.routes.<route_name>` 配置 route 后，通过当前插件的 AI capability 调用：

```python
ai = context.capabilities.ai
if ai is None:
    raise RuntimeError("AI capability 尚未就绪")

result = await ai.complete(
    "chat",
    [{"role": "user", "content": "你好"}],
    required_modalities=("text",),
)
reply = result.content
```

Provider、模型、fallback、重试和总超时由统一 AI 注册表管理。`pinned_model` 用于管理员诊断，`list_models()` 返回满足模态要求的可用 profile。插件获得 route 结果、profile、尝试次数与模型元数据。

### 同步库

同步阻塞调用通过 `run_sync()` 提交：

```python
from core.plugin_base import run_sync

result = await run_sync(blocking_function, argument)
```

`run_sync()` 继承当前插件执行 gate，使用共享 worker、按插件 bulkhead 和全局公平队列。

---

## 💾 数据持久化

```python
from pathlib import Path

data_file = context.data_dir / "state.json"
data_file.parent.mkdir(parents=True, exist_ok=True)
```

持久文件采用以下实践：

- 明确 schema 与版本
- 原子替换写入
- 有界文件大小
- 启动时校验
- 备份与恢复路径
- 用户或群作用域隔离

Core 提供原子存储、有界文件缓存和路径工具，插件可按数据类型复用。

简单 JSON 状态可直接复用原子 helper：

```python
from core.plugin_base import load_json, write_json

state = load_json(data_file, default={})
state["count"] = int(state.get("count", 0)) + 1
write_json(data_file, state)
```

---

## 🧩 声明式服务与 capability

插件间调用通过 Core 维护的服务契约完成。Manifest 的 `services` 声明导出，`uses_services` 声明消费，Core 校验服务所有者、调用方和 capability。

```json
{
  "services": [
    {
      "name": "voice.synthesize_text",
      "callback": "synthesize_text",
      "callers": ["smalltalk"]
    }
  ]
}
```

服务声明字段：

| 字段 | 说明 |
|---|---|
| `name` | Core 服务表中的稳定服务名 |
| `callback` | 入口模块中的公开异步函数名 |
| `callers` | Core 服务表规定的调用插件集合 |
| `required_capability` | 特定桥接需要的附加 capability，普通服务省略 |

当前服务集合为 `voice.synthesize_text`、`chat.reply`、`codex.enqueue_arxiv_summary` 和 `core.observe_outgoing_action`。Core 按服务名核验固定所有者和调用方。

Manifest capability 集合：

| 名称 | 用途 |
|---|---|
| `admin_sessions` | 高权限会话在管理员身份变化时收口 |
| `config_subscription` | 接收当前插件配置 revision |
| `execution_timeout_exempt` | 由插件生命周期控制长任务超时 |
| `onebot_media` | 查询受控 OneBot 消息与媒体 |
| `secret_admin` | 管理员 secret 命令能力 |

Core 按插件规范名核验 capability 申请。新增跨插件桥接或 capability 时，同步更新 schema、Protocol、所有者、调用方、Manifest 和授权测试。

---

## 🔄 错误处理

插件按边界处理错误：

1. 输入校验错误返回清晰用法。
2. 外部服务错误返回插件级降级消息。
3. 数据完整性错误记录结构化上下文并保护原状态。
4. 取消信号沿协程传播到资源所有者。
5. 入口异常由 Core 转换为公开错误码与 request ID。

```python
try:
    payload = await fetch_data()
except TimeoutError:
    context.logger.warning("上游请求超时")
    return segments("服务响应超时，请稍后重试")
```

预期输入与业务异常使用固定、清晰的插件提示。未预期异常通过统一公开错误出口记录有界诊断并返回稳定错误码：

```python
from core.public_errors import public_error_response

try:
    return await perform_operation()
except Exception as exc:
    return public_error_response(
        context,
        exc,
        logger=context.logger,
        component="hello.handle",
    )
```

Shell、Jupyter、Codex 和远端管理等敏感操作使用 `core.sensitive_audit.log_sensitive_operation()`。命令正文、代码、路径和异常正文作为 `payload` 生成长度与 HMAC 指纹；日志字段保留稳定 operation、status、request ID、job ID、返回码和异常类型。

---

## ✅ 文档与测试契约

每个插件 README 采用统一顺序：

1. 功能简介
2. 使用条件
3. 命令入口
4. 配置与凭据
5. 数据与权限
6. 运行与排障
7. 开发验证

Manifest 中的 `examples` 与 `invalid_examples` 覆盖每个命令节点。插件测试至少覆盖：

- Manifest 解析与命令目录
- 正常参数、边界参数和错误输入
- 私聊、群聊与权限场景
- Session 生命周期
- 外部服务降级
- 初始化、重载和关闭
- 数据迁移与恢复

---

## 🧭 下一步

- 公开签名：[API 参考](05-api-reference.md)
- 高级运行模式：[高级主题](07-advanced.md)
- 完整插件目录：[插件目录](09-plugins.md)
