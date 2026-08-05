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
    └── utils.py        # 可选：工具函数
```

插件目录名必须与 `plugin.json` 的 `name` 一致，并且是小写 ASCII Python 标识符：只使用 `a-z`、数字和下划线，不能以数字开头。运行时只接受 `plugins/` 的真实直接子目录；插件目录、`plugin.json`、入口文件、被导入的 Python 包目录，以及 Core 分配的外置数据目录都不能是符号链接、junction 或其他 reparse point。目录名还应避免与标准库或三方包重名，例如 `json`、`requests`、`github`，以免插件代码中的非相对导入发生遮蔽。

`entry` 必须是规范 POSIX 相对路径，并以小写 `.py` 结尾，例如 `main.py` 或 `handlers/main.py`。绝对路径、盘符或 UNC 路径、反斜杠、空段、`.`、`..`、Windows ADS/保留名，以及不能逐段映射为 Python 标识符的路径都会在 Manifest 校验时拒绝。入口必须是插件真实目录内的普通文件；不要用链接把共享代码或仓库外文件当作入口。共享逻辑应放入可安装的框架模块，或复制为插件目录内受版本控制的源码。

### 导入规范

插件会被加载为 `plugins.<插件名>` 下的规范 Python 包。入口和插件内部的延迟导入都由 source-only loader 从已经核验的插件根目录读取；运行时不会执行 `__pycache__` 中的 `.pyc`。插件内部模块使用**相对导入**。

加载前，框架会形成稳定、带资源上限的不可变快照：全部 `.py`、插件根目录的 `.json`、`plugin.json`，以及 `watch_files` 显式声明的嵌套普通文件。根目录下的 `.json` 属于代码授权面，改动会触发重载；运行时状态严禁写在插件源码根目录，必须写入源码树外的 `context.data_dir`，否则会形成“写状态→重载”的自激循环。嵌套配置若会影响代码行为，必须列入 `watch_files`；单次快照最多 4096 个文件、512 个源码目录、65536 个扫描条目和 128 MiB，总 Python 源码最多 64 MiB、单个源码最多 8 MiB、单个观察文件最多 64 MiB；超限会拒绝该代，而不是形成不完整指纹。

插件是与 Bot 进程同权限运行的受信任 Python 扩展，能够直接访问 Python 进程能力。`PluginContext` 的配置与 secret 视图虽然按插件命名空间收窄，但这只是接口最小化，不是进程安全边界。source-only loader、路径检查和 execution gate 用于保证代际一致性与框架入口的发布/卸载原子性，不构成针对恶意插件的沙箱。只安装经过代码审查的插件；不要向用户承诺可以安全安装或运行陌生第三方插件。

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

## 🚀 从零到运行：五步创建一个插件

下面用一个「待办清单」插件 `todo` 演示完整开发流程：目录、Manifest、入口、加载和测试。它带一个子命令和一份 JSON 持久化，覆盖大多数轻量插件需要的能力。

### 第 1 步：创建目录与文件

```text
plugins/todo/
├── plugin.json
├── main.py
└── __init__.py      # 可为空，使插件成为规范 Python 包
```

目录名 `todo` 必须与 `plugin.json` 的 `name` 完全一致，且是小写 ASCII 标识符。

### 第 2 步：编写 plugin.json

`commands` 是命令目录的**唯一真相来源**：`/help`、JSON 导出、权限/场景校验和全插件测试都读取它。声明触发词、子命令、用法与示例，不要再在帮助字符串里维护第二份清单。

```json
{
  "name": "todo",
  "version": "1.0.0",
  "description": "极简待办清单",
  "entry": "main.py",
  "commands": [
    {
      "name": "todo",
      "triggers": ["todo", "待办"],
      "help": "管理个人待办",
      "usage": "/todo <add|list> [内容]",
      "examples": ["/todo list", "/todo add 写周报"],
      "invalid_examples": ["/todo unknown"],
      "subcommands": [
        {
          "name": "add",
          "help": "添加一条待办",
          "usage": "/todo add <内容>",
          "examples": ["/todo add 写周报"],
          "invalid_examples": ["/todo add"]
        },
        {
          "name": "list",
          "help": "列出全部待办",
          "usage": "/todo list",
          "examples": ["/todo list"],
          "invalid_examples": ["/todo list extra"]
        }
      ]
    }
  ]
}
```

### 第 3 步：编写 main.py

用 `context.command_invocation` 消费 Core 已经解析好的子命令路径，而不是自己 `split`；用 `context.data_dir` 做插件私有持久化。

```python
import json
from typing import Any, Dict, List

from core.plugin_base import segments


def _store(context) -> "Path":
    context.data_dir.mkdir(parents=True, exist_ok=True)
    return context.data_dir / "items.json"


def _load(context) -> list[str]:
    path = _store(context)
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def _save(context, items: list[str]) -> None:
    _store(context).write_text(
        json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8"
    )


async def handle(
    command: str, args: str, event: Dict[str, Any], context
) -> List[Dict[str, Any]]:
    invocation = context.command_invocation          # Core 解析好的目录路径
    sub = invocation.node.name if invocation else ""  # "add" / "list" / "todo"
    rest = invocation.arguments if invocation else args.strip()

    items = _load(context)

    if sub == "add":
        if not rest:
            return segments("❌ 用法: /todo add <内容>")
        items.append(rest)
        _save(context, items)
        return segments(f"✅ 已添加：{rest}（共 {len(items)} 条）")

    if sub == "list" or sub == "todo":
        if not items:
            return segments("📝 暂无待办，用 /todo add <内容> 添加")
        body = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(items))
        return segments(f"📝 待办清单（{len(items)}）\n{body}")

    return segments("❓ 未知子命令，用 /todo list 或 /todo add <内容>")
```

### 第 4 步：加载插件

- 开发期直接 `python main.py` 重启，进程启动时会加载 `plugins/` 下全部合规插件。
- 运行中让管理员发送 `/reload` 触发插件热扫描（无需重启进程），或 `/reload config` 只重读配置。
- 若开启了 `enable_plugin_watcher`，保存文件后框架会在稳定快照核验通过后自动重载。

加载成功后 `/plugins` 会列出 `todo`，`/help todo` 会显示刚声明的命令目录。

### 第 5 步：测试

```text
用户: /todo add 写周报
机器人: ✅ 已添加：写周报（共 1 条）

用户: /todo list
机器人: 📝 待办清单（1）
        1. 写周报

用户: /todo
机器人: 📝 待办清单（1）
        1. 写周报
```

提交前的本地验证：

```bash
python -m compileall -q plugins/todo
python -m pytest tests -q          # 若为插件补充了测试
git diff --check
```

完整并行回归可在项目根目录执行 `pytest -n 2`。测试代码通过
`subprocess.run()`/`Popen()` 读取文本输出时，若启用 `text=True`，必须同时显式声明
`encoding="utf-8"` 和解码错误策略（项目统一使用 `errors="replace"`），避免结果依赖
Windows、Git Bash 或 POSIX 主机的系统默认编码。

> [!TIP]
> 需要多轮引导（如逐步询问截止时间）用 [会话方法](#-会话方法多轮对话)；需要定时推送用 [schedule 字段](#-schedule-字段) 与 [07-advanced.md](07-advanced.md#定时任务)；需要调用大模型用 [统一 AI/VLM route](#-统一-aivlm-route)。

---

## 📋 plugin.json 配置

### 完整字段

```json
{
  "name": "myplugin",
  "version": "1.0.0",
  "description": "插件描述",
  "entry": "main.py",
  "watch_files": ["config/settings.json"],
  "enabled": true,
  "concurrency": "parallel",
  "services": [],
  "uses_services": [],
  "capabilities": [],
  "dependencies": [
    {
      "name": "aiohttp",
      "required": true,
      "description": "入口导入链直接使用的 HTTP 客户端"
    },
    {
      "name": "PIL",
      "required": false,
      "description": "仅图片子功能需要；缺失时文本功能仍可运行"
    }
  ],
  
  "commands": [
    {
      "name": "cmd",
      "triggers": ["cmd", "命令"],
      "help": "命令帮助文本",
      "usage": "/cmd <list|show> [参数]",
      "examples": ["/cmd list"],
      "invalid_examples": ["/cmd unknown"],
      "permission": "public",
      "contexts": ["private", "group"],
      "priority": 0,
      "subcommands": [
        {
          "name": "list",
          "aliases": ["ls", "列表"],
          "help": "列出条目",
          "usage": "/cmd list [page:N]",
          "examples": ["/cmd list page:1"],
          "invalid_examples": ["/cmd list page:zero"]
        },
        {
          "name": "show",
          "help": "查看一个条目",
          "usage": "/cmd show <id>",
          "examples": ["/cmd show item-123"],
          "invalid_examples": ["/cmd show"]
        }
      ]
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
| `entry` | string | ✅ | 插件真实目录内的规范 POSIX `.py` 相对路径，通常是 `main.py`；禁止绝对路径、`..`、反斜杠和链接 |
| `watch_files` | string[] | ❌ | 额外纳入不可变快照的规范相对普通文件，最多 64 项；用于嵌套 JSON/静态配置，不要包含 `data/` |
| `description` | string | ❌ | 插件描述 |
| `enabled` | bool | ❌ | 是否启用，默认 `true` |
| `concurrency` | string | ❌ | `parallel`（默认）或 `sequential` |
| `services` | array | ❌ | 当前插件向 Core 导出的受控服务；服务名、回调、调用方和附加能力必须与 Core 闭集契约完全一致 |
| `uses_services` | string[] | ❌ | 当前插件需要消费的受控服务；只有 `_SERVICE_CONTRACTS` 声明的调用方可请求 |
| `capabilities` | string[] | ❌ | 当前插件需要的 Core 特权；插件名与能力必须符合 `_CAPABILITY_CONTRACTS` 的闭集映射 |
| `dependencies` | array | ❌ | 可导入 Python 模块的 preflight 契约；`required: true` 缺失时拒绝加载，`false` 只告警并继续加载 |
| `commands` | array | ❌ | 命令列表 |
| `schedule` | array | ❌ | 定时任务列表 |

`dependencies[].name` 必须写 Python 的 import 名，而不是 PyPI 发行名，例如 Pillow
写成 `PIL`、PyJWT 写成 `jwt`、scikit-learn 写成 `sklearn`。入口模块或其顶层
导入链会立即 import 的包应声明为 `required: true`；只有在功能分支中延迟导入、
缺失时有明确降级或安装提示的包才声明为 `required: false`。每项都应提供
`description`，说明依赖对应的运行能力，避免把可选功能误报成整插件硬依赖。

`version` 使用插件自己的 SemVer 代号，不要求与项目版本相同：修改 manifest 命令、权限、依赖契约或插件运行行为时递增，纯文档修改不必递增。版本号只用于代际诊断和日志，不要把它当作依赖比较或热更新授权条件。

`services`、`uses_services` 与 `capabilities` 是授权声明，不是可自由扩展的字符串。
Core 在加载 manifest 时同时校验服务的唯一提供者、精确调用方、所需附加能力，以及
每项特权允许由哪些插件请求；名字未知、调用方不匹配、冒充提供者或重复声明都会拒绝
整份 manifest。运行时上下文只接收已经通过这次校验的声明，因此把目录改成某个内置
插件名不会自动获得无限执行时间、管理员会话、密钥写入、OneBot 媒体或配置订阅能力。
若确实要新增跨插件服务或 Core 特权，必须同步修改 `core/models.py` 中的
`_SERVICE_CONTRACTS` / `_CAPABILITY_CONTRACTS`、相关 manifest 和边界测试。

### commands 字段

| 字段 | 类型 | 必需 | 说明 |
|------|------|------|------|
| `name` | string | ✅ | 命令名，传给 handle() 的 command 参数 |
| `triggers` | array | ✅ | 触发词列表 |
| `help` | string | ✅ | 简明说明，显示在统一命令目录中 |
| `usage` | string | 项目要求 | 可复制的完整用法 |
| `examples` | string[] | 项目要求 | 至少一条合法语法样例；会进入全插件 `/event` 门禁 |
| `invalid_examples` | string[] | 项目要求 | 至少一条错误语法样例；会进入全插件 `/event` 门禁 |
| `permission` | string | ❌ | `public`、`bot_admin` 或 `group_admin`，默认 `public` |
| `contexts` | string[] | ❌ | `private`、`group` 的允许集合，默认二者都允许 |
| `admin_only` | bool | ❌ | 顶层兼容字段；为 `true` 时等价于 `permission: bot_admin` |
| `priority` | int | ❌ | 优先级，越大越优先，默认 0 |
| `subcommands` | array | ❌ | 递归子命令目录 |

子命令节点使用 `name`、`aliases`、`help`、`usage`、`examples`、
`invalid_examples`、`permission`、`contexts` 和 `subcommands`。它还可设置
`match: "exact"`，表示该节点只在没有剩余参数时选中；默认 `prefix` 允许把剩余文本
交给业务参数解析器。`triggers` 只属于顶层命令，子命令使用 `aliases`。

Core 为每个节点生成全局稳定命令码：`<插件名>.<顶层 name>.<子命令 name>...`。
例如上面的两个叶节点分别是 `myplugin.cmd.list` 与 `myplugin.cmd.show`。帮助、JSON
导出、权限/场景校验和自动化测试都读取这棵树，不再从插件自定义帮助字符串反向猜命令。
单个插件最多 512 个命令节点、8 层、每层 128 个直接子节点；同级规范名与别名必须唯一。

运行时可用以下入口查看同一份目录：

```text
/help                         # 查看 Core 与已加载插件的功能导航
/help page 1                  # 按页浏览插件级导航
/help myplugin                # 查看插件完整子树
/help myplugin.cmd.show       # 按稳定命令码精确查询
/help json myplugin           # 导出包含权限、场景、样例和子节点的 JSON
/help json page 1             # 自动化按页读取所有命令节点
```

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
处理门控（私聊 / require_bot_name_in_group=false / has_prefix / 活跃会话）
    ↓
URL-only → url_parser（门控与静音之后，静音时跳过）
    ↓
只喊机器人名字或只 @ 机器人
    ↓
活跃会话并调用 handle_session()
    ↓
命令匹配并调用 handle()
    ↓
未知命令提示（仅严格命令前缀且首字母为字母）
    ↓
smalltalk 回落并调用 handle_smalltalk()
```

#### 插件与分发流程的交互

1. **命令处理**
   - 用户发送 `/your_command args`
   - 当前用户没有活跃会话消费该输入后，router 匹配到插件命令
   - Dispatcher 调用插件的 `handle()` 函数
   - 命令返回后不会继续进入会话或闲聊回落

2. **会话处理**
   - 用户在活跃会话中发送后续消息（包括与全局命令同名或纯空白的输入）
   - Dispatcher 优先调用插件的 `handle_session()`；返回 `None` 才继续全局命令匹配
   - 会话处理成功后不会继续进入闲聊回落

3. **闲聊处理**
   - 插件作为 `smalltalk_provider` 时
   - 只有消息通过门控、未命中命令、未被活跃会话消费、且群聊未静音时，Dispatcher 才调用 `handle_smalltalk()`
   - 插件根据上下文决定是否返回消息，返回 `[]` 表示不回复

#### 短路示例

```python
# 场景：用户在猜数字会话中，输入恰好与全局命令同名
# 用户的会话状态：guess_game = True

# 执行顺序：
# 1. 发现活跃猜数字会话
# 2. 调用 guess.handle_session()
# 3. 会话返回结果并直接结束本轮，不执行同名全局命令

# 场景：用户在会话中，但没有发送命令
# 用户的会话状态：guess_game = True

# 执行顺序：
# 1. 发现活跃会话
# 2. 调用 guess.handle_session()
# 3. 返回 ["太大了！"]
# 4. 直接返回，不进入命令匹配或 handle_smalltalk()
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

**复合命令使用 Core 解析结果**：

Dispatcher 会把当前请求的最长匹配路径放在 `context.command_invocation`。复合插件不应再
维护另一份顶层别名表；用 `resolve_context_command_invocation()` 读取规范子命令和未消费
参数即可。业务字段、自然语言和动态 ID 仍由插件自己的解析器负责。

```python
from core.router import resolve_context_command_invocation

async def handle(command: str, args: str, event: Dict, context) -> List:
    invocation = resolve_context_command_invocation(context, "myplugin.cmd", args)
    if invocation is None or len(invocation.chain) == 1:
        return segments("请使用 /help myplugin 查看完整命令目录")

    subcommand = invocation.chain[1].name       # 已解析为规范名，不是用户别名
    business_args = invocation.remainder_after(1)
    if subcommand == "list":
        return await handle_list(business_args, context)
    if subcommand == "show":
        return await handle_show(business_args, context)
    return segments("未知命令")
```

`CommandInvocation.node` 是最深命中节点，`chain` 是从根到该节点的完整路径，
`arguments` 是最深节点之后的业务参数。权限与私聊/群聊场景在调用插件前已经按该最深
节点执行，但插件仍须校验业务参数并为错误样例返回明确用法。

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
   - 由插件内部的 attention gate、硬频控、普通插话概率、PFC planner 和 reply checker 控制是否回复
   - `/xc`、私聊、`@`、直接叫名字、只喊名字后的追问、reply 引用小青、以及有近期上下文锚点的“她/ta”共指召唤会走 forced 路径

3. **与其他流程的关系**
   - `handle_smalltalk()` 是最后的回落路径
   - 活跃会话、命令、未知命令提示都先于 `handle_smalltalk()` 执行
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
# 配置：同一项的一次性读取可用 getter
context.get_config("option")    # 当前插件配置的分离副本
context.get_secret("api_key")   # 当前插件秘密的分离副本

# 同一次操作需要读取多项配置或同时读取配置与秘密时，只取一个原子快照
settings = context.get_settings_snapshot()
plugin_config = settings.plugin_config(context.plugin_name)
plugin_secrets = settings.plugin_secrets(context.plugin_name)
settings.revision                # 该代配置的单调修订号

# 路径
context.plugin_name  # str - 插件名
context.plugin_dir   # Path - 插件目录 (plugins/myplugin/)
context.data_dir     # Path - 外置数据目录（默认 data/myplugin/）

# 工具
context.logger       # Logger - 日志记录器（自动附带 request_id）
context.http_session # aiohttp.ClientSession - HTTP 客户端
context.metrics      # MetricsCollector | None - 运行指标收集器

# 当前消息上下文
context.current_user_id   # int | None
context.current_group_id  # int | None

# 插件私有运行时状态（同一插件代内跨事件共享；卸载、重载或进程重启会清空）
context.state        # Dict[str, Any]

# 需要跨卸载、重载或进程重启保留的数据必须写入外置目录
context.data_dir     # Path - context.state 不是持久化存储
```

### 常用方法

```python
# 获取默认发送群列表
groups = context.default_groups()

# 获取所有插件代共享的结构化命令目录
catalog = context.get_command_catalog()

# 获取所有插件
plugins = context.list_plugins()
```

### 原子配置快照

`get_config()` 和 `get_secret()` 各自适合一次性读取一个值。一个业务操作如果要读取多个值，尤其同时读取公开配置和秘密，必须先调用一次 `get_settings_snapshot()`，然后始终从这一个快照读取，避免配置热重载恰好发生在两次读取之间而混用两代设置：

```python
async def handle(command: str, args: str, event: Dict, context) -> List:
    settings = context.get_settings_snapshot()
    config = settings.plugin_config(context.plugin_name)
    secrets = settings.plugin_secrets(context.plugin_name)
    endpoint = config.get("endpoint")
    api_key = secrets.get("api_key")
    ...
```

快照及其中的映射不可变。不要直接读取 `context.config` 或 `context.secrets`，也不要把两次独立 getter 当成同一代。长期运行的管理器可以缓存已应用的 `settings.revision`：只接受更新的修订号，忽略过期回调；相同修订号若出现不同内容则应拒绝，以免并发重载覆盖新配置。`plugins/codex/manager.py` 展示了 revision 栅栏，`plugins/pendo/config.py` 展示了整组运行参数校验后一次发布；Pendo 的监听开关、地址、端口和安全选项因此不会出现半新半旧状态。

### 会话方法（多轮对话）

会话位于 dispatcher 线性流程中的命令匹配之前，用于实现模态多轮对话。

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
4. 用户后续消息优先进入会话处理
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

# 持久化读改写（同步或 async callback 均可）
await context.update_session(lambda working: working.set("step", 2))

# 结束会话
await context.end_session()

# 检查是否有会话
has = await context.has_session()
```

`create_session()` 和 `get_session()` 返回的都是隔离快照，修改返回对象不会写回存储；
持久修改必须放在 `update_session(callback)` 的 callback 中。框架为 callback 建立同一
`(user_id, group_id)` 键的事务工作副本：成功返回只提交一次，异常、`BaseException`、
值校验失败或取消都完整回滚。会话数据是有界的 JSON-like 树：只接受字符串键的内建
`dict`、`list`、`tuple` 和 `str/bytes/int/float/bool/None`，拒绝循环引用、自定义对象、
超过 64 层或 100,000 节点的数据，因而不会执行不可信的 `__deepcopy__` 钩子。
callback 不得返回已经调度的 `asyncio.Task`/`Future`；误返对象会先被取消并完整回收；
直接使用 `async def` callback 即可。callback 自己对同一键调用 get/peek/exists/create/delete
时看到暂存视图，嵌套 update 会被拒绝，callback 新建的子任务不会继承事务视图。
会话过期时 `get_session()`/`has_session()` 只清理旧项并分别返回 `None`/`False`；框架不会
因为下一条消息自动创建会话。需要继续流程时必须再次显式调用 `create_session()`。

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
            await context.update_session(lambda working: working.set("step", 2))
            return segments("太小了！再试试")
        elif guess > target:
            await context.update_session(lambda working: working.set("step", 2))
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

# 在 update_session 的工作副本内设置或删除数据
def mutate(working):
    working.set("key", value)
    return working.delete("obsolete_key")

removed = await context.update_session(mutate)

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
    await context.update_session(lambda working: working.set("attempts", attempts))
    
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

1. **会话优先级**：活跃会话先于全局命令和闲聊；返回 `None` 才回落到命令匹配
2. **超时自动清理**：超过 timeout 时间会话自动删除
3. **每个用户独立**：每个 `(user_id, group_id)` 组合有独立的会话
4. **手动结束**：游戏结束时必须调用 `context.end_session()`

#### 长任务不要滥用 Session

框架 session 适合“下一条消息就是当前流程输入”的交互，例如猜数字、表单填写、SSH 交互和 Pendo 记账引导。它不适合承载长时间运行的后台任务，因为活跃 session 会优先接管同一用户后续消息，容易影响全局命令、闲聊或其他普通输入。

如果插件需要后台执行并在完成后主动通知，建议像 `codex` 插件一样在插件内部维护自己的会话标签和任务队列：

1. 用普通命令创建业务会话，例如 `/codex create main cwd:C:/project`。
2. 后续命令显式带标签，例如 `/codex main <任务>`，插件立即返回“已收到”。
3. 插件内部按标签串行、跨标签并行执行任务。
4. 任务完成后用 `context.send_action(build_action(...))` 主动发送结果。
5. 运行时状态写入 `context.data_dir`，例如默认根目录下的 `data/codex/sessions.json`、`session/<label>/conversation.jsonl`、任务图片 artifacts 和删除归档；不要从 `__file__` 拼接源码目录。

如果另一个插件需要触发后台任务，也可以像 `arxiv_filter` 一样把自身主响应和后台侧路分开：先正常返回用户需要立刻看到的结果，再用 `asyncio.create_task()` 或插件内部队列投递长任务。长任务失败时单独发送失败消息，不能阻塞主响应。

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
remaining_minutes = context.get_mute_remaining(group_id)  # float，单位：分钟
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

`parse()` 只把单个 ASCII 字母的短选项（如 `-f`）和以 ASCII 字母开头的长选项（如 `--output-format`）视为选项。因此 `-1+2`、`-12:34:56`、`-3σ` 等科学文本会保留为位置参数。如果需要传入形如 `-f` 或 `--mode=step` 的字面文本，在它前面放置 `--` 终止选项解析。

只需要引号分词、但仍有业务自定义语义校验时，使用 `core.args.tokenize()`，不要在插件内再导入 `shlex`。`tokenize()` 对未闭合引号会抛出 `ValueError`，命令入口应转成明确的用户语法错误。

对搜索式、笔记、自然语言问题等必须保留引号、反斜杠和内部空格的自由文本，只用 `split(maxsplit=n)` 切出固定的命令前缀，然后原样传递剩余字符串；不要用 `parse().rest()` 重建这类文本。

简单命令不需要 `parse()`；当命令有多个可选参数或选项时，`parse()` 能避免重复的选项分割逻辑。

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

生命周期和热路径回调优先写成 `async def`；如果一个回调确实是纯 CPU/内存轻量同步函数，也可以保留 `def`，Core 会把同步 callback 放入插件同步 bulkhead。不要在同步 callback 中做未卸载的文件、数据库、网络或子进程 I/O；这条规则比机械地把所有函数改成 async 更重要。

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
        headers={"Authorization": "Bearer <SERVICE_TOKEN>"}
    ) as resp:
        result = await resp.json()
    
    return segments(f"结果: {result}")
```

### 统一 AI/VLM route

需要 LLM/VLM 的插件不要自己读取统一 provider 密钥，也不要重复实现 Chat Completions HTTP、重试或 fallback。先在 `config.json` 的 `plugins.<插件名>.ai.routes` 声明命名 route，再通过当前插件的 capability 调用：

```python
async def summarize(context, text: str) -> str:
    ai = context.capabilities.ai
    if ai is None:
        return ""

    result = await ai.complete(
        "summary",
        [
            {"role": "system", "content": "请用中文简要概括。"},
            {"role": "user", "content": text},
        ],
        temperature=0.3,
        max_tokens=400,
    )
    context.logger.info(
        "AI summary completed profile=%s attempts=%s",
        result.profile,
        result.attempts,
    )
    return result.content
```

```json
{
  "plugins": {
    "my_plugin": {
      "ai": {
        "routes": {
          "summary": {
            "models": ["primary-text", "backup-text"]
          }
        }
      }
    }
  }
}
```

capability 在 core 中固定当前插件名，所以插件只能调用自己的 route。`models` 从前到后组成 fallback 链；视觉调用还应传 `required_modalities=("text", "image")`。`pinned_model` 适合显式的管理员诊断开关，固定后不会偷偷切到其他模型。API Base、模型 profile 和密钥的完整结构见 [06-configuration.md](06-configuration.md#统一-aivlm-注册表)。

### 收窄第三方文本与图片

第三方 JSON、HTML、RCON 或 SSH 字段进入 OneBot 消息前，使用 `core.plugin_base.bounded_external_text()`。它是 ANSI、控制字符、字符上限和 UTF-8 字节上限的唯一实现；插件只保留自己的字段提取与业务无效值规则，不再复制截断算法。需要完整协议值（时间戳、坐标、ID）时使用 `truncate=False`，不要解析被截断的前缀。

下载图片后使用 `core.image_validation.validate_image_bytes()`，并把 HTTP MIME 映射成 `expected_format`；缓存或其它本地路径使用 `validate_image_path()`，不要在 `is_file()` 后直接 `Image.open()`。共享校验会检查真实格式、容器尾部、字节/尺寸/像素/帧预算、Pillow 解压炸弹，并在 `verify()` 后重新打开逐帧解码；本地版本还拒绝符号/硬链接并复核打开前后的文件身份。Pillow 解码是同步工作，异步插件应通过下节的 `run_sync()` 调用。

### 处理同步库

某些库（如 `requests`）是同步的，必须通过框架的有界同步 offloader 运行：

```python
from core.plugin_base import run_sync
import requests

async def handle(command: str, args: str, event: Dict, context) -> List:
    # 使用当前插件的同步 bulkhead，不阻塞事件循环
    response = await run_sync(requests.get, "https://api.example.com")
    return segments(response.text)
```

`run_sync()` 不只是 `asyncio.to_thread()` 的别名。它把调用登记到当前插件的 execution gate：

- `sync_parallel_limit` 限制单个插件同时占用的 worker，保留其它插件的前进空间；
- `sync_queue_limit` 和全局 `global_sync_queue_limit` 对等待任务设硬上限，队列满时快速报告可观察的过载错误；
- 同一插件的同步任务按提交顺序调度，不同插件之间轮转，避免一个插件用长任务独占四个共享 worker；
- 调用方取消后，尚未启动的任务不会再执行；已经进入 Python 线程的函数无法被强制终止，gate 会继续跟踪它，unload/reload 会等待有界 drain，超时则隔离旧代而不会并装新代。

普通插件入口不要直接使用 `asyncio.to_thread()` 或默认 executor，因为它们绕过这些配额、过载和卸载语义。只有确需自管线程的底层组件才应使用专用的有界 executor，并且必须在 `init()` 创建、在 `shutdown()` 停止接纳并有界 drain；这类 executor 不得与框架共享默认线程池。

异步 HTTP 与有状态同步 HTTP 的边界也要保持明确：无会话的异步请求优先使用 `context.http_session`，公开图片/HTML 下载使用已有的 `fetch_public_bytes()` 安全边界；只有上游协议确实要求 Cookie、访客握手或 `requests.Session` 时，才使用 `requests_request_bounded()`，并把整段同步流程包进 `run_sync()`。这种会话可以在当前插件代的 `context.state` 中按 TTL 复用，但必须在 `shutdown()` 关闭；不要每条消息重建握手，也不要把同步会话直接放到事件循环上。

---

## 💾 数据持久化

### 使用 data_dir

每个插件有独立的数据目录：

框架只会创建并复用 `data_root/<插件名>/`（默认即项目根的 `data/<插件名>/`），并在每次构造 context 时复核数据根和插件子目录的身份。不要把它们替换为 symlink、junction、挂载别名或其他 reparse point；身份发生变化时该 context 会 fail closed。插件源码树中不应再创建 `data/`：升级时仅把它视为一次性迁移源，成功复制到新权威目录后会移到 `data_root/.legacy-plugin-data/<插件名>/` 供人工回退，运行时不会双读。源码、Manifest 和 `watch_files` 不应写入运行期状态。

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
      "api_key": "<MYPLUGIN_API_KEY>",
      "endpoint": "https://api.example.com"
    }
  }
}
```

真实密钥只能放在未跟踪的 `config/secrets.json` 或部署环境变量中，不能写入插件默认值、测试和文档。提交前应检查 `git diff --cached`，确认没有把密钥、Token、Webhook 或带签名的 URL 加入版本库；文档示例统一使用 `<MYPLUGIN_API_KEY>`、`${SERVICE_TOKEN}` 等完整占位符。

文件以 8 KiB 固定分块扫描，跨块匹配仍保留完整逻辑行和正确行号；单逻辑行最多 4 MiB。超过上限时立即产生不可 allowlist 的 `LogicalLineTooLongError` 并失败关闭，不会先把任意大的无换行文件读入内存。

确需保留兼容性 fixture 时，可在仓库根目录 `.secret-scan-allowlist.json` 使用精确条目；四个字段均必需，且 `fingerprint` 必须是完整 SHA-256：

```json
{
  "entries": [
    {
      "path": "path/to/fixture.txt",
      "rule_id": "credential.assignment.token.v1",
      "fingerprint": "<FULL_64_CHARACTER_SHA256>",
      "reason": "legacy interoperability fixture"
    }
  ]
}
```

Allowlist 只匹配完全相同的路径、规则和指纹；PEM 私钥指纹覆盖从 `BEGIN` 到匹配 `END` 的完整规范化块，而不是公共头部。秘密删除或内容变化后条目会变为 stale 并使扫描失败，必须同步删除或审查更新。任何目标文件无法打开、读取中断或扫描期间身份发生变化也会形成脱敏的结构化 `scan-error` 并失败关闭，不能用 allowlist 跳过。

### 在插件中读取

`context.get_secret(path)` 只从当前插件的秘密命名空间读取，并返回与内部快照分离的值。
插件不能读取全局管理员列表或其他插件秘密。单值读取使用 getter；需要与公开配置保持同代时使用上文的原子快照：

```python
async def handle(command: str, args: str, event: Dict, context) -> List:
    api_key = context.get_secret("api_key")

    if not api_key:
        return segments("错误：未配置 API Key")

    # 使用配置
    ...
```

---

## 📝 日志记录

使用 `context.logger`：

```python
from core.sensitive_audit import summarize_sensitive

async def handle(command: str, args: str, event: Dict, context) -> List:
    payload = summarize_sensitive(args)
    context.logger.info(
        "request accepted command=%s payload_kind=%s payload_length=%d "
        "payload_bytes=%d payload_fingerprint=%s actor=%s",
        command,  # manifest 中的稳定命令名，不是用户参数
        payload.kind,
        payload.length,
        payload.byte_length,
        payload.fingerprint,
        event.get("user_id"),
    )
    return segments("OK")
```

`DEBUG` 仍属于普通日志，不能写完整或截断的命令、Prompt、聊天历史、URL、路径、认证信息或模型响应。`summarize_sensitive()` 返回字符/字节长度和进程内可关联、重启即轮换的 HMAC 指纹；它不会保留原文，也不能用于授权、缓存或持久化标识。不要用无密钥 SHA-256 代替，因为短命令可被离线枚举。异常应按下文“错误处理”接入统一脱敏 helper，不能使用 `logger.exception()` 或 `exc_info=True`。

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

    if not context.is_global_admin(user_id):
        return segments("你没有权限执行此操作")
    
    # 执行管理员操作
    ...
```

---

## 🛠️ 错误处理

### 基本模式

```python
from core.public_errors import public_error_response

async def handle(command: str, args: str, event: Dict, context) -> List:
    try:
        result = await do_something(args)
        return segments(f"成功: {result}")
    except KnownInputError:
        # 仅返回由本插件定义、内容固定且经过审查的业务提示；不要回显
        # 第三方库或系统异常的 str(exc)。
        return segments("参数格式无效，请按 /example help 中的格式重试")
    except Exception as exc:
        return public_error_response(
            context,
            exc,
            logger=context.logger,
            component="example.handle",
        )
```

`public_error_response()` 会向用户返回稳定错误码和本次 `request_id`，并将经过脱敏和长度限制的异常链写入日志。不要使用 `logger.exception()`、`exc_info=True`，也不要把 `str(exc)`、URL、路径、认证头或 secret 拼进回复；这些写法会绕过统一脱敏边界。框架 Dispatcher 还有同样的兜底，但插件入口主动使用 helper 可以保留准确的 component。

需要执行敏感操作（例如管理员提交的代码、命令或远端任务）时，公开错误出口仍按上面的两类选择：可预期且文案固定的业务/输入错误直接返回经过审查的 `segments(...)`；未预期异常使用 `public_error_response()`，不要把敏感载荷或系统路径放进回复。`public_error_response()` 是用户错误出口，不会替代操作审计。

敏感操作审计统一使用 `core.sensitive_audit.log_sensitive_operation()`：它只记录安全的 operation/status/request/job 标识、异常类型、稳定的长度和进程内 HMAC 指纹；命令、代码、路径和异常正文只能作为 `payload` 进入摘要，不能直接插入日志格式串。若需要返回码等稳定数值，使用 helper 的命名参数；不要在插件中重新实现字段白名单、指纹或异常类型过滤。Shell 和 Jupyter 的审计格式以此为共同边界。

### 优雅降级

```python
from core.public_errors import public_error_message

async def handle(command: str, args: str, event: Dict, context) -> List:
    # 尝试主要方案
    try:
        result = await primary_api()
        return segments(result)
    except Exception as exc:
        public_error_message(
            context,
            exc,
            logger=context.logger,
            component="example.primary_api",
        )
    
    # 降级到备用方案
    try:
        result = await backup_api()
        return segments(result)
    except Exception as exc:
        public_error_message(
            context,
            exc,
            logger=context.logger,
            component="example.backup_api",
        )
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
        api_key = context.get_secret("api_key")
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

3. **异步 I/O 与有界同步桥接**
   ```python
   # ❌ 不好：阻塞主线程
   def handle_sync(...):
       time.sleep(5)  # 阻塞 5 秒
       ...
   
   # ✅ 好：使用异步
    async def handle_async(...):
        await asyncio.sleep(5)  # 不阻塞
        ...

    # 同步库必须经插件 bulkhead；不要直接 asyncio.to_thread(...)
    result = await run_sync(blocking_library_call, arg)
    ```
