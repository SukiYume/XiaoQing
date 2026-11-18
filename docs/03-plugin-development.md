# 🧩 03 - 插件开发指南

中文 | [English](en/03-plugin-development.md)

本章说明 XiaoQing 插件的目录结构、Manifest、运行入口、Context、会话、调度、服务能力和验证流程。
接口签名速查见 [API 参考](05-api-reference.md)。

---

## 🔐 插件信任模型

插件是与 Bot 同进程、同操作系统权限运行的受信任 Python 扩展。
部署者负责插件来源与代码审查。
Core 为插件提供作用域配置、secret、数据目录、capability、执行预算和生命周期管理。

插件执行 gate 负责并发、排队、超时、熔断和代际排空。
安全部署采用可信插件集合、最小系统权限、回环网络与受控反向代理。

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

运行中的管理员可发送 `/reload` 重新扫描插件。
启用 `enable_plugin_watcher` 后，文件快照变化会触发热重载或 restart-only 提示。

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

全仓门禁与代码约定见 [代码与验证约定](#-代码与验证约定)。

---

## 📏 代码与验证约定

项目支持 Python 3.11 及以上版本。
Python 代码采用局部赋值对齐、100 列内的紧凑表达和解释业务边界的中文注释。
公共 API、数据格式和异常语义由实现与回归测试共同约束。

### 排版

使用 `python scripts/format_code.py` 格式化全部受版本控制及新建的代码文件，使用 `python scripts/format_code.py --check` 执行只读检查。
也可在命令后指定文件或目录。
Python 格式器先调用 Ruff 收敛表达式布局，再对齐同缩进、相邻逻辑块中的赋值与多行关键字参数。
转换前后校验 AST 一致性。
JavaScript 与 PowerShell 通过 Pygments 定位声明赋值，并验证转换前后的非空白 token 一致。
HTML、CSS、Shell 与 VBS 纳入文件清单并保留原有语法布局。

```python
owner_id    = event.user_id
timeout     = settings.timeout
retry_limit = settings.retry_limit
```

独立逻辑块用空行分开。
条件、调用和容器能在行宽内清晰表达时保持紧凑。
复杂表达式保留分行。
字符串和注释内容由词法边界保护。
JavaScript、HTML 和 PowerShell 保留各语言语法与现有缩进，局部对齐只用于相邻且含义相关的声明。

### 去重与注释

相同状态归属、失败语义和生命周期的实现可以抽取到公共工具。
不同协议、资源所有者和事务边界保持明确。
删除不可达分支、失效兼容代码与无用导入时，同时核对调用者、清单入口、动态加载和测试引用。

中文注释说明时间与金额单位、取消期间的资源所有权、原子提交顺序、缓存失效、权限边界和兼容约定。
函数名已经清晰表达的简单动作可保持简洁。
测试注释说明触发条件、预期契约和隔离方式。

### 验证与文档

```bash
python scripts/format_code.py --check
python -m ruff check .
python -m mypy core plugins
python -m pytest -q -n auto --cov=core --cov=plugins --cov-report=term-missing
git diff --check
```

先运行受影响模块的测试，再执行全仓库门禁。
测试覆盖当前功能的行为契约；业务修复补充或调整对应功能的用例，不单独保留针对某次修复的测试。
测试数据使用临时目录或临时数据库。

测试按当前功能组织：`tests/core` 覆盖核心行为，`tests/transport` 覆盖协议与传输，`tests/plugins/<plugin>` 覆盖插件，`tests/integration` 覆盖组件协作，`tests/tooling` 覆盖测试辅助工具与打包资源。
用例验证输入、输出、状态变化、错误处理、权限、并发和资源生命周期，相同执行流程使用参数化合并。
用例从公开入口进入：命令、服务、定时任务和模块的公开函数。
替身只放在边界上：网络和外部服务、模型网关、消息发送、时钟、随机数、子进程、进程全局的日志配置、操作系统和第三方库。
Core 和插件自己的函数、常量和模块状态不替换，只有这几种例外：
要把并发停在某个确切位置时，在那一处包一层屏障，放行后照常调用原函数；
要在真实输入走不到的确切位置验证失败处理时，在那一处注入失败；
模块级的锁、缓存和后台任务句柄在测试之间重置；
指向固定文件位置的常量换成临时目录；
超时、轮询间隔这类等待时长调短，测试不真的等；
上限要喂很大的数据才碰得到时，调低那个上限；
可选依赖是否可用的标志换成“不可用”，验证缺依赖时的行为。
Web 客户端测试执行实际 JavaScript。
多个测试文件共用的替身和辅助放在 `tests/helpers/`，测试文件之间不互相导入。
断言以运行结果和有效数据契约为依据。
提示词、人设、帮助文字和常量的原话，以及文档措辞、注释、固定供应商域名、已删除文件清单和源码实现形式，交由代码审查维护。
测试中的样例 URL 用于验证解析、配置传递与访问策略。

重复代码检查随测试一起跑（`tests/tooling/test_check_redundancy.py`，单独运行就是 `python -m pytest tests/tooling/test_check_redundancy.py`）：`core`、`plugins`、`scripts` 和 `main.py` 里不能有两个函数体一模一样的函数（函数体不到 8 行的小函数，以及只有 `...`、`pass` 或 `raise NotImplementedError` 的接口声明不算）。出现时留一份、其余改成调用它；确实要各留一份的，在这个测试文件的 `ALLOWED` 里写明原因。

README 描述当前用法和配置。
ARCHITECTURE 描述模块职责、数据流与所有权。
项目手册描述公共契约。
命令、权限和别名与 `plugin.json` 保持一致，跨模块公共约定通过链接引用。

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

运行数据写入 `context.data_dir`。
发行资源通过 setuptools package-data 进入 wheel/sdist，并由资源契约测试校验。

入口路径使用插件目录内的规范 POSIX `.py` 相对路径，例如 `main.py` 或 `handlers/main.py`。
源码与资源路径保持在插件真实目录内。

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

Core 在导入插件前检查 `dependencies` 中的模块可用性。
部署环境通过 `requirements.txt`、项目 extra 或自身包管理流程安装依赖。

依赖对象格式：

```json
{
  "name": "PIL",
  "required": true,
  "description": "图片处理"
}
```

`required: true` 的模块决定插件加载条件。
`required: false` 适合保留帮助或降级功能的可选模块。

依赖字段：

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `name` | string | 必填 | Core 不导入即可定位的 Python 模块名，不能以 `plugins` 开头 |
| `required` | boolean | `true` | 模块缺失时控制插件加载结果 |
| `description` | string / `null` | `null` | 依赖用途说明 |

`dependencies` 描述 Python 导入前置条件。
插件之间的协作使用 `services` 与 `uses_services`，加载事务会统一核验所有者、调用方和服务可用性。

### 命令字段

顶层命令对象：

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `name` | string | 必填 | 稳定命令名，用于命令 code |
| `triggers` | string[] | 必填 | 非空且唯一的顶层触发词。匹配区分大小写 |
| `help` | string | 必填 | 功能摘要 |
| `usage` | string | `/<首个 trigger>` | 完整用法 |
| `admin_only` | boolean | `false` | 顶层 Bot 管理员快捷声明。`true` 归一化为 `bot_admin` |
| `permission` | `public` / `bot_admin` / `group_admin` | `public` | 权限级别 |
| `contexts` | `private` / `group` 数组 | 两者 | 使用场景 |
| `priority` | integer | `0` | 顶层路由优先级，数值较大者先匹配 |
| `examples` | string[] | `[]` | 正确样例，最多 16 条 |
| `invalid_examples` | string[] | `[]` | 参数、场景或权限错误样例，最多 16 条 |
| `effect` | `read` / `write` | 不声明 | 命令只查询还是会新增、修改、删除数据，见下文 |
| `subcommands` | array | `[]` | 子命令节点 |

子命令节点：

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `name` | string | 必填 | 同级唯一的规范名 |
| `aliases` | string[] | `[]` | 同级唯一的别名 |
| `help` | string | 必填 | 功能摘要 |
| `usage` | string | 必填 | 完整用法 |
| `match` | `prefix` / `exact` | `prefix` | `prefix` 保留后续业务参数。`exact` 只接受完整节点路径 |
| `permission` | 权限枚举 | 继承父节点 | 子节点可收紧权限 |
| `contexts` | 场景数组 | 继承父节点 | 子节点可收窄场景 |
| `examples` | string[] | `[]` | 正确样例 |
| `invalid_examples` | string[] | `[]` | 错误样例 |
| `effect` | `read` / `write` | 不声明 | 不继承父节点 |
| `subcommands` | array | `[]` | 下一层节点 |

整棵命令目录最多 8 层、512 个节点。
Core 将父子权限合成为较严格级别，并取父子场景交集。
`group_admin` 接受当前群群主、当前群管理员和 Bot 管理员。
`bot_admin` 接受 Bot 管理员。

Router 依次比较 `priority` 和触发词长度。
两个插件声明相同触发词与相同优先级时，加载事务报 `CommandConflictError` 并保留已发布插件代。

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

Core 为上述叶节点生成稳定 code `example.todo.add`，由插件名 `example`、命令名 `todo` 和子命令名 `add` 组成。
插件通过 `context.command_invocation` 获取已解析节点和剩余参数。

#### 读写性质

`effect` 说明命令会不会改动数据：`read` 只查询，`write` 会新增、修改或删除。Agent 这类调用方按这项声明统一处理所有插件，不需要为某个插件写专门代码。
小青 agent 在同一个任务里不会把完全相同的 `write` 命令执行第二遍，读命令照常重复执行。
命令文本完全相同、每次执行却有不同效果的命令（比如逐步撤销）也照实声明 `write`；需要重复时 agent 会先向发起人确认。

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
| `id` | string / `null` | `null` | 插件内唯一的任务 ID。省略时使用 `handler` |
| `delivery` | `broadcast` / `targeted` / `silent` | `broadcast` | Core 执行的投递模式 |
| `group_ids` | positive integer[] / `null` | `null` | 投递群。省略时使用 `default_group_ids` |
| `description` | string / `null` | `null` | 任务说明 |
| `enabled` | boolean | `true` | 任务启用状态 |

Scheduler 在插件发布时校验 handler、cron、任务 ID、投递模式和目标。
任务到期时，Core 按以下模式处理：

- `broadcast`：把 handler 返回的消息段投递给该 schedule 的全部目标群；
- `targeted`：接收 `ScheduledDelivery` 目标消息，校验群目标属于该 schedule 的群列表，并支持受控私聊目标；
- `silent`：执行 handler 并保持零投递目标，忽略返回载荷，同时拒绝该调度上下文的主动发送。

`broadcast` 与 `targeted` 的目标群取以下来源中第一个写了的：部署配置 `config.plugins.<插件名>.schedules.<任务 ID>.group_ids`、条目中的 `group_ids`、全局 `default_group_ids`。
任务 ID 是条目的 `id`，没写 `id` 时是 `handler`；部署配置里写空列表表示这个任务不发往任何群。
目标群保持 `group_ids` 列表中的顺序，在每次执行时读取，改 config 后 `/reload` 即生效。
`plugin.json` 随仓库发布，不写真实群号；只属于某个部署的目标群写在部署配置里。
`silent` 不声明 `group_ids`。
插件重载会替换该插件拥有的调度任务。

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

简单位置参数可直接处理 `args`。
带引号、选项或整数边界的命令使用 Core 解析器：

```python
from core.args import parse, parse_int, tokenize

parsed = parse(args)
target = parsed.first
limit = parse_int(parsed.opt("limit"), minimum=1, maximum=100)
verbose = parsed.has("v")
words = tokenize(args, strict=True)
```

`parse()` 支持单字母短选项、ASCII 长选项、`--key=value` 和 `--` 选项终止符。
`tokenize()` 默认把未闭合引号按空白切分。
命令协议需要完整引号时使用 `strict=True`。
`parse_int()` 接受 ASCII 十进制整数，并返回经过范围校验的 `int` 或 `None`。
完整返回结构见 [API 参考的参数解析](05-api-reference.md#-参数解析)。

### `handle_session()`

当前会话键存在活动 Session 时调用：

```python
async def handle_session(text, event, context, session): ...
```

处理函数读取传入快照，并通过 `context.update_session()` 提交持久修改。

### `close_session()`

Core 结束会话时调用，插件在这里回收连接、后台任务等资源；没有实现时会话直接结束：

```python
async def close_session(event, context, session): ...
```

以下情况会调用它：

- 用户发送退出词。
- 此人不再能使用该插件的任何顶层命令。
- `handle_session()` 抛出异常。
- 命令执行器丢弃它新开的一次性会话。

这里抛出的异常只被记录，会话照常结束。

### `handle_smalltalk()`

插件作为 `smalltalk_provider` 时调用：

```python
async def handle_smalltalk(text, event, context): ...
```

### `observe_message()`

`smalltalk_provider` 可选提供。Core 在处理门控之后、URL 阶段之前调用它，让 provider 观察没有被跳过的消息，返回值被忽略：

```python
async def observe_message(clean_text, event, context): ...
```

URL、命令、带命令前缀的文本和活动会话的输入被跳过，不会被观察。

### `handle_url()`

名为 `url_parser` 的插件可提供：

```python
async def handle_url(url, event, context): ...
```

### `call_bot_name_only()`

当前 `smalltalk_provider` 可处理只包含 Bot 名称的消息：

```python
async def call_bot_name_only(context):
    return segments("我在")
```

只叫名字（正文只有 Bot 名称，或只 `@` 机器人、没带图）多半是在看机器人在不在，要快：Core 调用当前 `smalltalk_provider` 的这个入口，不走 `handle_smalltalk()`。`xiaoqing_chat` 从 `bot_name_only_replies` 里随机回一句，发出后记进聊天记录，这个人接下来一分钟内的那句算在跟她说。

### 入口选择顺序

完成消息清洗和群聊门控后，Dispatcher 按消息形态选择入口，群静音期间跳过 `handle_url()` 和 `handle_smalltalk()`：

1. 纯 URL 调用 `handle_url()`。
2. 只包含 Bot 名称时调用 `call_bot_name_only()`。
3. 活动会话调用所属插件的 `handle_session()`；返回 `None` 时继续命令路由。
4. 命令目录命中后调用 `handle()`。
5. 普通文本回落到当前 `smalltalk_provider` 的 `handle_smalltalk()`。

以命令前缀开头、前缀后第一个字符是字母而没有匹配命令的消息，由 Core 返回统一提示。
会话、命令和 URL 输入受观察边界保护，闲聊 provider 只观察符合消息策略的普通对话。

### 生命周期

```python
async def init(context): ...


async def shutdown(context): ...
```

`init()` 创建插件所有资源。
`shutdown()` 按创建顺序的逆序关闭任务、连接、数据库和 Web 服务。
插件代际发布会在初始化成功后提交入口。

### 调度函数

```python
async def scheduled_daily(context):
    return segments("早上好")
```

`broadcast` 调度 Context 使用 system principal 和声明的发送目标，返回消息段由 Scheduler 投递到全部目标群。
各群内容需要独立生成时使用 `targeted`：

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

`settings.config` 包含安全共享字段与当前插件公开命名空间，`settings.secrets` 限定在当前插件命名空间，其中只保留敏感业务映射。
`plugin_config()` 与 `plugin_secrets()` 直接取得业务映射。
Core capability 承担管理员 secret、OneBot 媒体和配置订阅等特权操作。

时间与当前调用的有效投递目标来自同一 Core 上下文：

```python
now = context.now()
group_ids = context.default_groups()
```

在 `broadcast` 与 `targeted` 定时任务中，`context.default_groups()` 返回该 schedule 经 Core 解析后的目标群。
`silent` 定时任务返回空列表。
命令与生命周期调用返回全局 `default_group_ids`。

### 日志与指标

插件用标准库 logger 加 `core.log.LogEvent` 写日志，`PluginContext` 不提供日志器：

```python
import logging

from core.log import LogEvent

log = logging.getLogger(__name__)

log.info(LogEvent("reply.done", chat_id=chat_id, elapsed_s=1.23, reply=text))
log.warning(LogEvent("fetch.failed", source="baike", error=exc))
log.info(LogEvent("cache.cleared"))
```

logger 名就是模块名，形如 `plugins.<插件名>.<模块>`，[日志配置](06-configuration.md#️-日志配置)的 `levels` 按这个名字或它的前缀 `plugins.<插件名>` 设级别。
`LogEvent(name, **fields)` 在构造时完成脱敏和渲染，日志行的消息部分是事件名加字段，例如 `reply.done chat_id=123 elapsed_s=1.23 reply="<17ch #5948c5c6>"`。
字段按传入顺序输出，值里有空格、引号或 `=` 时加 JSON 引号，没有字段时只输出事件名。
对象里只留脱敏后的结果，`str()`、`repr()` 和格式化失败时的兜底输出都拿不到原始值。

事件名用 ASCII 小写，按 `<领域>.<动作>[.<结果>]` 点分，例如 `reply.done`、`fetch.failed`、`proactive.skip`。
事件名不带插件名，日志行里的 logger 名已经说明来源。

字段的值按下表输出，从上到下取第一条符合的：

| 值 | 输出 |
|---|---|
| `None` | 省略该字段 |
| 布尔 | `true` / `false` |
| 数字：整数、浮点数和其他数值类型（`IntEnum`、`Decimal`、`Fraction`、numpy 标量） | 普通数字，例如 `count=3`、`ratio=0.25` |
| `date`、`datetime`、`time` | ISO 文本，例如 `day=2026-11-17` |
| 异常实例或 `core.log.ExceptionSummary` | 展开成类型、位置和状态码等几个字段，见下文 |
| `dict` | 逐项展开，键写成 `parent.child` |
| 列表、元组、集合 | `[N items]` |
| 字节 | 长度和指纹 |
| 凭据键或正文键下的字符串 | 长度和指纹 |
| 其他键下整串符合 `[A-Za-z0-9_.:/@+-]{1,128}` 的字符串 | 原样 |
| 其他字符串（含空格、中文、控制字符或超过 128 个字符）和其他对象（`Path`、自定义对象等） | 长度和指纹；`Path` 的指纹和 `str(path)` 的相同 |

键名按整串判断，展开后的键也一样：

- 凭据键：按 `_` 和 `.` 拆开后，任一部分是 `token`、`secret`、`password`、`passwd`、`cookie`、`auth`、`authorization`、`credential`、`apikey` 或 `key`。
- 正文键：按 `.` 拆成段，任一段是正文段，整个键就是正文键，所以正文键下展开的每一项都脱敏（`payload.file_name` 跟着 `payload`）。正文段是：整段为 `name`、`nickname`、`sender_name`、`user_name`、`display_name` 或 `card`；或者这段不以 `_id`、`_code`、`_count`、`_kind`、`_type`、`_status`、`_name` 结尾，且按 `_` 拆开后任一部分是 `text`、`content`、`message`、`msg`、`history`、`dialogue`、`reply`、`prompt`、`response`、`raw`、`output`、`input`、`thinking`、`reason`、`goal`、`hint`、`description`、`marker`、`term`、`emotion`、`summary`、`evidence`、`path`、`url`、`uri`、`host`、`payload`、`command`、`cmd`、`query`、`title`、`preview`、`body` 或 `detail`。所以 `reason_code`、`command_name`、`message_type` 按普通键处理。

脱敏后的值写成 `<17ch #5948c5c6>`（17 个字符的文本）或 `<10B #5948c5c6>`（10 字节）；它和 `[N items]` 都含空格，在日志行里带 JSON 引号，`fields` 里的值不带引号。
指纹是进程随机密钥下的 HMAC 前 8 位十六进制：同一进程里同一个值的指纹相同，可以用来关联；重启后密钥更换，跨重启无法关联。

异常写成下面几个字段（以键 `error` 为例），异常消息不写：

- `error`：类型名。
- `error.at`：出错位置 `文件:行`。取回溯里最深一帧插件代码（`plugins/`）；没有插件代码时取最深一帧项目内其他代码（`core/`、`scripts/`、`main.py`），再没有时取最深一帧真实文件；异常没有回溯时省略。
- `error.cause`：`__cause__` / `__context__` 链最里层那个异常的类型名；没有链或链被抑制（`raise … from None`）时省略。
- 状态码一类的属性，异常带着时才有：`error.status`（HTTP 状态码，也从 `response.status_code`、`response.status` 读）、`error.errno`、`error.winerror`、`error.returncode`、`error.lineno`、`error.colno`、`error.sqlite`（SQLite 错误名）、`error.module`（导入失败的模块名）。

例如 `fetch.failed source=baike error=AIRequestError error.at=plugins/hello/main.py:31 error.cause=ConnectionResetError error.status=504`。
`core.log.summarize_exception(exc)` 返回同样内容的 `ExceptionSummary`，把它作为字段值时渲染成一样的字段。

按这些规则写字段：

- QQ 号、群号、`chat_id`、`message_id`、`job_id` 等标识直接传入，明文可见。
- `status=ok`、`route=chat`、`plugin=pendo` 这类标签取不含空格的 ASCII 值，明文可见。
- 消息正文、回复、提示词、命令、路径、URL、主机名、昵称等自由文本放在正文键下，只留长度和指纹。
- 程序生成的原因写 `reason_code=`；`reason` 是正文键，值会被脱敏。
- 异常写 `error=exc`，留下类型、位置、异常链最里层的类型和状态码一类的属性，异常消息不进日志。
- token、Cookie、授权头不写进日志；确实需要关联时放在凭据键下。

request ID 由 Core 设置，插件不在消息或字段里写它。
Core 在消息进入时生成 ID（主动 WebSocket 在客户端收到事件时，入站 HTTP 每个请求一个，入站 WebSocket 每帧一个），另给每条经 `command_runner` 代为执行的命令和每次定时任务运行各设一个。
Core 收到消息、插件处理、回复发送和处理失败的日志行都在 request ID 列带同一个 ID；`asyncio.create_task`、`asyncio.to_thread` 和 `run_sync()` 复制上下文，后台任务沿用发起它的请求的 ID。
插件生命周期钩子（`init`、`shutdown`）在没有 request ID 的作用域里调用。

常驻循环在某个请求里第一次启动，之后服务多个请求（例如按队列运行任务的 worker）。
它在入口处用 `log_scope(None)` 清掉启动它的那个请求的 ID，处理每个单元时用该单元记下的 request ID 另开作用域：

```python
from core.log import log_scope


async def _worker(self) -> None:
    with log_scope(None):
        while job := await self._next_job():
            with log_scope(job.request_id):
                await self._run(job)
```

Core 的入站调度 worker 和主动 WebSocket 按发送者排队的 drainer 也是这样：自己不带 ID，每个事件在它自己的 ID 下处理。

级别约定：

| 级别 | 用途 |
|---|---|
| `DEBUG` | 排障细节：原始帧、传输层细节、可选的诊断明细 |
| `INFO` | 一件事做完或状态变化：收到消息、命令完成、一次定时任务结束、连接建立；xiaoqing_chat 的步骤日志 |
| `WARNING` | 已处理的降级：兜底、上游超时或网络错误、坏数据被跳过、配置缺失导致功能不可用 |
| `ERROR` | 需要人处理：数据损坏、配置无效、清理失败、真正意外的异常（公开错误出口）；网络和上游故障不算 |

同一件事在一个调用点只记一行。

测试用 `caplog` 读日志。
`tests/helpers/assertions.py` 的 `logged_events(caplog, name)` 按记录顺序返回事件，断言读 `fields` 里脱敏后的值；需要真实日志文件时用 `tests/helpers/app_test_support.py` 的 `logged_lines` fixture。

```python
import logging

from tests.helpers.assertions import logged_events


async def test_timeout_is_logged(caplog):
    caplog.set_level(logging.WARNING, logger="plugins.hello")
    ...
    (failed,) = logged_events(caplog, "fetch.failed")
    assert failed.fields["error"] == "TimeoutError"
```

Core 统一计量插件入口的调用次数、耗时、错误和取消。

### 插件代内状态

`context.state` 是当前插件代共享的内存字典，适合缓存和任务注册表。
持久状态写入 `context.data_dir`。

### 权限与静音

命令的使用者和使用场景由 `plugin.json` 的 `admin_only`、`permission` 和 `contexts` 声明，Core 在调用插件前通过 `core.router.command_access` 检查，插件代码不自行判断管理员。
需要区分权限的分支（比如只给管理员的写操作）声明成单独的子命令，处理时确认调用来自这个子命令。
检查路径与示例见 [权限模型](07-advanced.md#-权限模型)。

```python
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

`context.principal` 保存用户、群、会话类型和投递目标。
`context.capabilities` 保存 Core 按插件声明与当前身份签发的窄能力。
`is_bot_admin` 只用于对象层面的规则，命令能不能用由 Manifest 声明、Core 判断，见 [权限模型](07-advanced.md#-权限模型)。
主动 OneBot Action 使用 `await context.send_action(action)`。
管理型插件可通过自身命令权限调用 `context.reload_config()` 或 `context.reload_plugins()`。
完整签名与 capability 属性见 [API 参考](05-api-reference.md#-plugincontext)。

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

同一会话键串行执行。
群聊键由群 ID 与用户 ID 组成，私聊键由用户 ID 组成。
Session 超时后结束。
后续业务可再次调用 `create_session()` 开始新流程。
用户发送 `退出`、`取消`、`exit`、`quit` 或 `q` 时，Core 调用 `close_session()` 后结束会话，这条消息不会到达 `handle_session()`。
每次继续前 Core 按 [权限模型](07-advanced.md#-权限模型) 复核此人此刻仍能用这个插件的至少一条顶层命令，撤权或场景不再允许时会话关闭。

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

QQ 按纯文本显示回复。回复使用纯文本，需要强调时使用 emoji 或独立行。Markdown 的 `**加粗**`、反引号、代码块和 `#` 标题会作为字面文本显示。
导出成 `.md` 文件的内容和发给模型的提示词不受此限。

入口返回值支持：

- 字符串
- OneBot 消息段列表
- 带发送回执的消息段 `DeliverySegments`
- Action 列表
- `None`，表示该调用不返回消息

业务状态要等消息真正发出才提交时，返回 `DeliverySegments` 并附上 `DeliveryReceipt`：

```python
from core.delivery import DeliveryReceipt, DeliverySegments


def commit() -> None:
    mark_sent(receipt.message_ids)  # 发出后各条消息的 ID；长回复被拆成几条时每条都有


def unknown() -> None:
    # 已交给传输层但没有回执：消息可能已经发出，也可能没有，message_ids 可能为空。
    # 不能当作确认送达，也不能回滚到可以重新领取的状态，否则可能重复投递。
    mark_delivery_unknown()


receipt = DeliveryReceipt(
    expected_actions = 1,
    commit           = commit,
    rollback         = release_reservation,
    unknown          = unknown,
)
return DeliverySegments(reply, receipt)
```

Core 等全部物理消息确认后调用 `commit`，明确失败调用 `rollback`，结果未知调用 `unknown`。
未知结果需要业务自己决定取舍：公告类可以按 at-most-once 视为完成以避免重复，
需要可靠送达的业务应保留可查询的“未知”状态，并在帮助和 README 中写明这一策略。
回调都至多执行一次。
主动发送使用 `send_with_receipt(context.send_action, action, receipt)`。
长文本由 Core 按消息段边界自动拆分，也可用 `split_message_segments()` 自行拆分。
出站文件与远程媒体遵循 Core 的类型、大小、路径和网络预算。

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

应用拥有并关闭共享 Session。
插件负责每次请求的超时、重定向、响应大小、内容类型和解析结构预算。

聊天内容控制请求目标时使用 `core.safe_http.fetch_public_html()` 或 `fetch_public_bytes()`：

```python
from core.safe_http import fetch_public_html

response = await fetch_public_html(url, timeout_seconds=10)
```

安全 HTTP 客户端逐跳校验 scheme、DNS、目标网段和重定向，并固定已验证地址。
返回正文受压缩与解压预算约束。

同一个模块里还有几件各插件共用的小工具：

| 名称 | 用途 |
|---|---|
| `BROWSER_USER_AGENT` | 需要以浏览器身份请求时使用的 User-Agent |
| `validated_proxy_url(value, *, allow_path=False)` | 校验配置里的代理地址，原样返回合法值，否则抛 `ValueError`（错误信息不含凭据） |
| `split_https_url_on_hosts(url, hosts)` | 地址是指向名单内主机的 https 地址（不带用户信息、默认端口）时返回拆开的结果，否则返回 `None`；只看写法，不做 DNS |

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

`bounded_external_text()` 统一处理 ANSI、控制字符、字符数和 UTF-8 字节数。
`validate_image_bytes()` 与 `validate_image_path()` 校验真实格式、容器、尺寸、像素、帧数和文件身份。
图片解码等同步工作通过 `run_sync()` 进入插件执行预算。

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

Provider、模型、fallback、重试和总超时由统一 AI 注册表管理。
`pinned_model` 用于管理员诊断，`list_models()` 返回满足模态要求的可用 profile。
插件获得 route 结果、profile、尝试次数与模型元数据。

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

插件间调用通过 Core 维护的服务契约完成。
Manifest 的 `services` 声明导出，`uses_services` 声明消费，Core 校验服务所有者、调用方和 capability。

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

当前服务集合包括 `voice.synthesize_text`、`chat.reply`、`codex.enqueue_arxiv_summary` 和 `core.observe_outgoing_action`。
小青 agent 增加四条桥：

- `xiaoqing_agent` 向 `xiaoqing_chat` 提供 `agent.handle_turn` 与 `agent.report_delivery`，后者回报当轮回复是否发出。
- `codex` 向 `xiaoqing_agent` 提供 `codex.agent_task`。
- `qingssh` 向 `xiaoqing_agent` 提供 `qingssh.agent_command`。

小青网上生活增加两条桥：

- `xiaoqing_life` 向 `xiaoqing_chat` 提供 `life.turn_context` 与 `life.export_memes`。数据只关于小青自己，不按用户身份门控，系统主体也可用。

小青统一记忆增加四条桥，都由 `xiaoqing_memory` 提供，经 `capabilities.memory` 使用：

- `memory.record` 由 `xiaoqing_chat`、`xiaoqing_agent`、`xiaoqing_life` 调用。
- `memory.recall` 由 `xiaoqing_chat`、`xiaoqing_agent` 调用。
- `memory.search` 与 `memory.forget` 由 `xiaoqing_chat` 调用。

Core 把调用方名字、主体类型和用户主体所在的会话写进载荷，调用方填的同名字段无效，系统主体也可用。
Core 按服务名核验固定所有者和调用方。
`agent.handle_turn` 与 `command_runner` 签发给用户消息的上下文，`command_runner` 以发起人身份执行命令、照常按各命令的权限与场景检查。
`codex.agent_task`、`qingssh.agent_command` 和本机工具授权 `local_tools` 分别按 `/codex`、`/ssh`、`/shell` 在 plugin.json 里的声明签发，每次调用都会复核。
`onebot_actions` 按 `/qq` 的声明签发给 `xiaoqing_chat`，同样每次调用复核；它走带响应的 OneBot 请求通道，不经过出站消息的观察和回执，发消息仍用 `context.send_action`。
`codex.agent_task` 与 `qingssh.agent_command` 的执行请求可以只在插件执行通道内启动工作并返回 `asyncio.Task`，Core 在通道外等待结果，调用方取消时一并取消该 Task。
这样串行插件不会被一条长命令占住。
`command_runner` 在一次性的会话空间里执行命令，不读取也不替换用户正在进行的交互会话。
命令新开的会话先交给插件的 `close_session` 回收资源再丢弃。
命令返回的 `DeliverySegments` 连同发送回执原样交回调用方，由它随实际发送结算，`run(..., operation_id=...)` 让每次调用有独立的请求 ID。
`run(..., request_texts=...)` 传入发起人本人这次说的原话（按先后，最后一条最新，Core 只保留最近 16 条、每条截断到 4000 字），命令在 `context.request_texts` 里读到。只在确有必要时用它（比如实体设备的出粮份数要对得上原话）；一般的写入按参数执行，和用户直接发送时一样，不拿原话做字面比对。用户直接发命令时 `context.request_texts` 为空，普通消息事件不能带入这个值。
发送回执在提交前收集这条回复每条物理消息的 ID（`DeliveryReceipt.message_ids`，长回复被拆成几条时每条都有），插件在提交回调里把状态和真实消息对应起来，不必事后回填。

Core 派生能力（密钥管理、QQ 操作、arXiv 的 Codex 摘要、agent 的本机工具、SSH 和 Codex）与会话续接使用同一套命令权限，全部经过 `core.router.command_access`，细则见 [权限模型](07-advanced.md#-权限模型)。

Manifest capability 集合：

| 名称 | 用途 | 可申请的插件 |
|---|---|---|
| `command_runner` | 以发起人身份执行已发布命令，权限与场景检查同本人直接发送 | `xiaoqing_agent` |
| `local_tools` | 本机执行代码、读写文件前向 Core 复核发起人仍能用 `/shell` | `xiaoqing_agent` |
| `config_subscription` | 接收当前插件配置 revision | `pendo` |
| `execution_timeout_exempt` | 由插件生命周期控制长任务超时 | `codex`、`jupyter`、`qingssh`、`shell` |
| `inbound_backlog` | 查询当前事件的发送者在本会话里已到达、还没开始处理的消息 | `xiaoqing_chat` |
| `onebot_actions` | Bot 管理员经 `/qq` 调用 OneBot 动作并取回响应 | `xiaoqing_chat` |
| `onebot_media` | 查询受控 OneBot 消息与媒体 | `xiaoqing_chat` |
| `secret_admin` | 管理员 secret 命令能力 | `bot_core` |

Core 按插件规范名核验 capability 申请。
新增桥接或 capability 需要同步更新的内容见 [声明式服务](07-advanced.md#-声明式服务)。

---

## 🔄 错误处理

插件按边界处理错误：

1. 输入校验错误返回清晰用法。
2. 外部服务错误返回插件级降级消息。
3. 数据完整性错误记录结构化上下文并保护原状态。
4. 取消信号沿协程传播到资源所有者。
5. 入口抛出的未预期异常由 Core 转换成公开错误回复。

```python
try:
    payload = await fetch_data()
except TimeoutError as exc:
    log.warning(LogEvent("fetch.failed", source="upstream", error=exc))
    return segments("服务响应超时，请稍后重试")
```

预期输入与业务异常使用固定、清晰的插件提示。
未预期异常交给统一的公开错误出口，它记一条诊断日志，回复一行说明：

```python
from core.public_errors import public_error_response

try:
    return await perform_operation()
except Exception as exc:
    return public_error_response(context, exc, component="hello.handle")
```

回复写成 `操作失败：<原因>（<异常类型> @ <位置>；request_id：…）`，例如：

```text
操作失败：请求超时，请稍后重试（TimeoutError @ plugins/hello/main.py:4；request_id：3f9a1c2e）
操作失败：上游服务返回 HTTP 404，重试无效（HttpStatusError @ plugins/hello/main.py:8；request_id：3f9a1c2e）
操作失败：插件内部错误，重试无效，请联系管理员（KeyError @ plugins/hello/main.py:16；request_id：3f9a1c2e）
```

原因按异常链从外到里第一个能分类的异常定，异常类型和位置也取那一环；位置的规则同日志字段 `error.at`，没有回溯时只写类型。
`SafeHttpError`、`ResponseTransportError` 只转述下层错误，链里更深处有能分类的异常时让位给它。
一个异常按下表从上到下取第一条符合的类别：

| 类别（日志 `category`） | 异常 | 回复里的原因 |
|---|---|---|
| `model` | AI 网关的 `AIError` | 按 `AIRequestError.category` 细分，例如模型服务超时、限流、鉴权失败、模型不可用，并说明重试有没有用；`AIConfigError` 是模型配置无效 |
| `timeout` | `TimeoutError`、`requests` 的 `Timeout`、`subprocess.TimeoutExpired` | 请求超时，请稍后重试 |
| `bad_response` | JSON 解析失败、有界 HTTP 的响应格式、大小和重定向错误、`SafeHttpError`、aiohttp 的 `ContentTypeError`、`requests` 的 JSON、解压和重定向错误 | 上游返回的数据无法解析，可稍后重试 |
| `upstream_status` | `HttpStatusError`、aiohttp 的 `ClientResponseError`、`requests` 和 `urllib` 的 `HTTPError` | 上游服务返回 HTTP <状态码>；408、425、429 和 5xx 说请稍后重试，其他状态说重试无效 |
| `network` | `ConnectionError`、`ResponseTransportError`、aiohttp 的 `ClientError`、`requests` 的 `RequestException`、`URLError`、DNS 和 SSL 错误、paramiko 的连接错误 | 网络连接失败，请稍后重试 |
| `dependency` | `ImportError` | 缺少依赖 <模块名>，请联系管理员 |
| `database` | `sqlite3.Error` | `SQLITE_BUSY`、`SQLITE_LOCKED` 时数据库正忙，请稍后重试；其余数据库操作失败，请联系管理员 |
| `io` | 其他 `OSError`、`subprocess.SubprocessError` | 文件或系统操作失败，请联系管理员 |
| `internal` | 其余异常 | 插件内部错误，重试无效，请联系管理员 |

`public_error_response()` 与 `public_error_message()` 都在 `plugins.<插件名>` logger 上记一条 ERROR 级的 `public_error`，字段是 `component`、`category`、模型错误时的 `reason_code`（`AIRequestError.category`）和 `error=exc`，后面附回溯；request ID 与回复里的相同。
日志的 `error` 是传进来的那个异常，按 [日志与指标](#日志与指标) 的规则展开；回复里的类型和位置取决定分类的那一环，两者可能不同。
异常消息不进回复，也不进日志。
失败不该打扰用户时（后台任务、静默降级），用 `log_public_error()` 只记这条日志、不回复。

Shell、Jupyter、Codex 和远端管理等敏感操作同样用 `LogEvent` 记录，例如 `log.info(LogEvent("execute", status="started", payload=command))`。
命令正文、代码和路径放进 `payload`，只留长度和指纹；`status`、`job_id`、返回码照常可见；异常用 `error=exc`，留下类型、位置和状态码一类的属性，并记 ERROR 级。

---

## ✅ 文档与测试契约

各类文档的职责见 [验证与文档](#验证与文档)。
每个插件 README 采用统一顺序：

1. 功能简介
2. 使用条件
3. 命令入口
4. 配置与凭据
5. 数据与权限
6. 运行与排障
7. 开发验证

Manifest 中的 `examples` 与 `invalid_examples` 覆盖每个命令节点。
插件测试至少覆盖：

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
- 完整插件目录：[插件使用手册](09-plugins.md)
