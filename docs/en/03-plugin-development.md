# 🧩 03 - Plugin development

[中文](../03-plugin-development.md) | English

This chapter covers plugin directories, manifests, runtime entry points, contexts, sessions, schedules, service capabilities, and verification.
See the [API reference](05-api-reference.md) for interface signatures.

## 🔐 Plugin trust model

Plugins are trusted Python extensions. They run in the Bot process with the same operating-system permissions.
Deployers manage plugin sources and review code.
Core supplies scoped configuration, secrets, data directories, capabilities, execution budgets, and lifecycle management.

Plugin execution gates manage concurrency, queues, timeouts, circuit breaking, and generation draining.
Secure deployment uses trusted plugins, minimum system permissions, loopback networking, and controlled reverse proxies.

## 🚀 Create a plugin in five steps

The example creates a `hello` plugin. Chinese text in examples keeps the actual command and response contract.

### 1. Create the directory

```text
plugins/hello/
├── __init__.py
├── plugin.json
└── main.py
```

Use the same directory name and manifest `name`. Use a lowercase ASCII Python identifier.

### 2. Write the manifest

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

### 3. Write the entry point

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

Use relative imports for plugin submodules:

```python
from .formatters import format_greeting
```

### 4. Load the plugin

Start the project:

```bash
python main.py
```

During operation, administrators can send `/reload` to scan plugins again.
When `enable_plugin_watcher` is enabled, file-snapshot changes trigger live reload or a restart-only notification.

### 5. Verify the plugin

```text
/plugins
/help hello
/hello 小青
```

Run these commands before submission:

```bash
python -m compileall -q plugins/hello
python -m pytest -q
python -m ruff check plugins/hello
python scripts/format_code.py --check plugins/hello
git diff --check
```

Use `python scripts/format_code.py --check` to check formatting across the repository before submission.
New plugins follow local assignment alignment, compact expressions, and Chinese comments at key business boundaries.

## Code and verification conventions

The project supports Python 3.11 or later.
Python code uses local assignment alignment, compact expressions within 100 columns, and Chinese comments that explain business boundaries.
Implementation and regression tests jointly define public APIs, data formats, and exception semantics.

### Formatting

Use `python scripts/format_code.py` to format tracked and new code files.
Use `python scripts/format_code.py --check` for a read-only check. You can append a file or directory.
The Python formatter first uses Ruff to normalize expression layout.

It then aligns assignments and multiline keyword arguments in adjacent logical blocks with the same indentation.
It checks AST equivalence before and after conversion.
JavaScript and PowerShell use Pygments to locate declaration assignments.
They verify that non-whitespace tokens remain identical after conversion.

The file list includes HTML, CSS, Shell, and VBS. These files keep their existing syntax layout.

```python
owner_id    = event.user_id
timeout     = settings.timeout
retry_limit = settings.retry_limit
```

Separate independent logical blocks with blank lines.
Keep conditions, calls, and containers compact when they fit clearly within the line width.
Keep complex expressions on separate lines. Lexical boundaries protect strings and comments.
JavaScript, HTML, and PowerShell keep their language syntax and existing indentation.
Use local alignment for adjacent declarations with related meanings.

### Shared code and comments

Implementations with the same state ownership, failure semantics, and lifecycle can share utilities.
Keep protocol, resource-ownership, and transaction boundaries explicit.
When you remove unreachable branches, obsolete compatibility code, or unused imports, check callers, manifest entries, dynamic loading, and tests.

Chinese comments explain time and currency units, cancellation ownership, atomic commit order, cache invalidation, permission boundaries, and compatibility contracts.
Keep comments concise for simple actions with clear function names.
Test comments explain trigger conditions, expected contracts, and isolation methods.

### Verification and documentation

```bash
python scripts/format_code.py --check
python -m ruff check .
python -m mypy core plugins
python -m pytest -q -n auto --cov=core --cov=plugins --cov-report=term-missing
git diff --check
```

Run affected-module tests first. Then run repository-wide checks.
Formatting operations check syntax-tree equivalence. Tests cover the behavior contracts of current features; a business fix adds or adjusts cases for the affected feature instead of keeping a test dedicated to one fix.
Test data uses temporary directories or databases.

READMEs describe current use and configuration.
ARCHITECTURE documents describe module responsibilities, data flow, and ownership.
The project manual describes public contracts.
Keep commands, permissions, and aliases consistent with `plugin.json`.

Link to shared conventions between modules.
Review records track issue disposition, repair batches, and final verification separately.
Mark a review closed after all work completes.

## 💾 Plugin directories and distribution resources

A common directory structure follows:

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

Core freezes these inputs for each loaded generation:

- All Python source
- JSON files in the plugin root
- Nested files declared in `watch_files`

Write runtime data to `context.data_dir`.
Setuptools package-data includes distribution resources in wheels and sdists. Resource-contract tests verify them.

Use a canonical POSIX relative `.py` path within the plugin directory for the entry point.
Examples include `main.py` and `handlers/main.py`.
Keep source and resource paths inside the real plugin directory.

## 🧩 Manifest reference

### Top-level fields

| Field | Type | Default | Description |
|---|---|---|---|
| `schema_version` | `1` | `1` | Manifest schema version |
| `name` | string | Necessary | Canonical plugin name, equal to its directory name |
| `version` | string | `0.0.0` | Plugin version |
| `description` | string | Empty | User-visible function summary |
| `author` | string | Empty | Author information |
| `entry` | string | `main.py` | Relative Python entry path |
| `enabled` | boolean | `true` | Startup loading switch |
| `concurrency` | `parallel` / `sequential` | `parallel` | Plugin-entry concurrency mode |
| `commands` | array | `[]` | Recursive command catalog |
| `schedule` | array | `[]` | Cron tasks |
| `dependencies` | array | `[]` | Python import dependencies |
| `watch_files` | array | `[]` | Additional snapshot files |
| `services` | array | `[]` | Declared service exports |
| `uses_services` | array | `[]` | Declared service consumers |
| `capabilities` | array | `[]` | Requests for narrow Core-managed capabilities |

Core checks module availability in `dependencies` before plugin import.
The deployment environment installs dependencies through `requirements.txt`, project extras, or its own package-management process.

Dependency object format:

```json
{
  "name": "PIL",
  "required": true,
  "description": "图片处理"
}
```

Modules with `required: true` determine plugin loading eligibility.
Use `required: false` for optional modules that permit help or reduced functionality to remain available.

Dependency fields:

| Field | Type | Default | Description |
|---|---|---|---|
| `name` | string | Necessary | Python module name that `importlib.util.find_spec()` can locate |
| `required` | boolean | `true` | Controls plugin loading when the module is missing |
| `description` | string / `null` | `null` | Dependency purpose |

`dependencies` describes Python import prerequisites.
Plugins collaborate through `services` and `uses_services`.
The loading transaction checks owners, callers, and service availability together.

### Command fields

Top-level command object:

| Field | Type | Default | Description |
|---|---|---|---|
| `name` | string | Necessary | Stable command name used in the command code |
| `triggers` | string[] | Necessary | Nonempty, unique top-level triggers. matching is case-sensitive |
| `help` | string | Necessary | Function summary |
| `usage` | string | `/<first trigger>` | Complete usage |
| `admin_only` | boolean | `false` | Top-level Bot administrator shortcut. `true` normalizes to `bot_admin` |
| `permission` | `public` / `bot_admin` / `group_admin` | `public` | Permission level |
| `contexts` | Array of `private` / `group` | Both | Allowed scenes |
| `priority` | integer | `0` | Top-level routing priority. larger values match first |
| `examples` | string[] | `[]` | Valid examples, maximum 16 |
| `invalid_examples` | string[] | `[]` | Invalid argument, scene, or permission examples, maximum 16 |
| `subcommands` | array | `[]` | Subcommand nodes |

Subcommand node:

| Field | Type | Default | Description |
|---|---|---|---|
| `name` | string | Necessary | Canonical name, unique among siblings |
| `aliases` | string[] | `[]` | Aliases, unique among siblings |
| `help` | string | Necessary | Function summary |
| `usage` | string | Necessary | Complete usage |
| `match` | `prefix` / `exact` | `prefix` | `prefix` keeps following business arguments. `exact` accepts only the complete node path. |
| `permission` | Permission enum | Parent value | A child can set stricter permissions. |
| `contexts` | Scene array | Parent value | A child can restrict scenes. |
| `examples` | string[] | `[]` | Valid examples |
| `invalid_examples` | string[] | `[]` | Invalid examples |
| `subcommands` | array | `[]` | Next-level nodes |

The complete command catalog permits at most 8 levels and 512 nodes.
Core combines parent and child permissions with the stricter level. It intersects their scene sets.
`group_admin` accepts the current group owner, current group administrators, and Bot administrators.
`bot_admin` accepts Bot administrators.

Router compares `priority`, then trigger length.
If two plugins declare the same trigger and priority, the loading transaction raises `CommandConflictError` and keeps the published generation.

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

Core generates stable code `example.todo.add` for this leaf node.
The plugin gets the parsed node and remaining arguments through `context.command_invocation`.

### Schedule fields

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

| Field | Type | Default | Description |
|---|---|---|---|
| `handler` | string | Necessary | Public asynchronous function name in the entry module |
| `cron` | object | Necessary | APScheduler `CronTrigger` arguments |
| `id` | string / `null` | `null` | Global task ID. Core generates a stable ID when omitted |
| `delivery` | `broadcast` / `targeted` / `silent` | `broadcast` | Delivery mode that Core executes |
| `group_ids` | positive integer[] / `null` | `null` | Target groups. omission uses `default_group_ids` |
| `description` | string / `null` | `null` | Task description |
| `enabled` | boolean | `true` | Task enabled state |

Scheduler validates handlers, cron expressions, task IDs, delivery modes, and targets when it publishes a plugin.
When a task becomes due, Core applies its delivery mode:

- `broadcast` sends the handler's returned segments to every target group for that schedule.
- `targeted` accepts `ScheduledDelivery` messages. It checks group targets against the schedule group list and supports controlled private targets.
- `silent` executes the handler with zero delivery targets. Core ignores its return payload and rejects proactive delivery from that schedule context.

`broadcast` and `targeted` use the entry's `group_ids`. Omitted or `null` values use global `default_group_ids`.
`silent` omits `group_ids`. Plugin reload replaces schedules owned by that plugin.

## ⌨️ Runtime entry points

### `handle()`

Core calls this entry after a command match:

```python
async def handle(command, args, event, context): ...
```

- `command`: stable top-level manifest command name.
- `args`: text after the top-level trigger.
- `event`: original OneBot event.
- `context`: context scoped to the current plugin, user, group, and request ID.

For compound commands, first read the parsed result:

```python
invocation = context.command_invocation
node_name = invocation.node.name
arguments = invocation.arguments
```

#### Argument parsing

Simple positional arguments can use `args` directly.
Use Core parsers for commands with quotes, options, or integer limits:

```python
from core.args import parse, parse_int, tokenize

parsed = parse(args)
target = parsed.first
limit = parse_int(parsed.opt("limit"), minimum=1, maximum=100)
verbose = parsed.has("v")
words = tokenize(args, strict=True)
```

`parse()` supports single-letter short options, ASCII long options, `--key=value`, and the `--` option terminator.
By default, `tokenize()` splits unclosed quotes on whitespace.
Use `strict=True` when complete quotes are necessary for the command protocol.
`parse_int()` accepts ASCII decimal integers. It returns a range-checked `int` or `None`.
See [argument parsing](05-api-reference.md#argument-parsing) for the complete return structure.

### `handle_session()`

Core calls this entry when the current session key has an active session:

```python
async def handle_session(text, event, context, session): ...
```

The handler reads the supplied snapshot. It commits persistent changes through `context.update_session()`.

### `handle_smalltalk()`

Core calls this entry when the plugin is the `smalltalk_provider`:

```python
async def handle_smalltalk(text, event, context): ...
```

### `handle_url()`

A plugin such as `url_parser` can supply this entry:

```python
async def handle_url(url, event, context): ...
```

### `call_bot_name_only()`

The current `smalltalk_provider` can handle messages that contain only the Bot name:

```python
async def call_bot_name_only(context):
    return segments("我在")
```

### Entry selection order

After message cleaning, group gating, and mute checks, Dispatcher selects an entry from the message form:

1. A URL-only message calls `handle_url()`.
2. A Bot-name-only message calls `call_bot_name_only()`.
3. An active session calls its plugin's `handle_session()`. A `None` result continues to command routing.
4. A command-catalog match calls `handle()`.
5. Ordinary text calls the current `smalltalk_provider` entry `handle_smalltalk()`.

Core returns a common message for unknown prefixed commands.
Observation boundaries protect session, command, and URL inputs.
The smalltalk provider observes ordinary conversation that satisfies message policy.

### Lifecycle

```python
async def init(context): ...


async def shutdown(context): ...
```

`init()` creates plugin-owned resources.
`shutdown()` closes tasks, connections, databases, and Web services in reverse creation order.
Generation publication commits entry points after successful initialization.

### Schedule functions

```python
async def scheduled_daily(context):
    return segments("早上好")
```

A `broadcast` schedule context uses a system principal and declared delivery targets.
Scheduler sends returned segments to all target groups.
Use `targeted` to generate independent content for each group:

```python
from core.delivery import ScheduledDelivery


async def scheduled_group_status(context):
    return [
        ScheduledDelivery.group(group_id, segments(f"群 {group_id} 状态正常"))
        for group_id in context.default_groups()
    ]
```

Use `silent` for maintenance tasks.
The handler returns `None` after data cleanup, cache refresh, or resource cleanup.

## 🧩 PluginContext

### Identity and paths

```python
context.plugin_name
context.plugin_dir
context.data_dir
context.current_user_id
context.current_group_id
context.principal
context.request_id
```

### Configuration and secrets

Read one item:

```python
model = context.get_config("ai.default_model")
api_key = context.get_secret("provider.api_key")
```

Read one atomic snapshot for an operation with several settings:

```python
settings = context.get_settings_snapshot()
config = settings.plugin_config(context.plugin_name)
secrets = settings.plugin_secrets(context.plugin_name)
revision = settings.revision
```

`settings.config` contains safe shared fields and the current plugin's public namespace.
`settings.secrets` contains sensitive business mappings from the current plugin's namespace.
`plugin_config()` and `plugin_secrets()` return business mappings directly.
Core capabilities handle privileged operations such as administrator secrets, OneBot media, and configuration subscriptions.

Time and effective delivery targets come from the same Core context:

```python
now = context.now()
group_ids = context.default_groups()
```

In `broadcast` and `targeted` tasks, `context.default_groups()` returns schedule targets resolved by Core.
In `silent` tasks, it returns an empty list.
Command and lifecycle calls return global `default_group_ids`.

### Logs and metrics

```python
context.logger.info("处理完成")
```

Plugin loggers automatically attach the request ID.
Log fields use bounded identifiers, counts, states, and durations.
Use summaries or redacted values for tokens, cookies, authorization headers, and sensitive user text.
Core measures plugin-entry call counts, durations, errors, and cancellations centrally.

### Generation-local state

`context.state` is an in-memory dictionary shared within the current plugin generation.
Use it for caches and task registries. Write persistent state to `context.data_dir`.

### Permissions and mute state

Declare command users and scenes in `plugin.json` through `admin_only`, `permission`, and `contexts`.
Core checks them before plugin calls with `core.router.command_access`.
Plugin code delegates administrator checks to Core.
Declare branches with different permissions, such as administrator-only writes, as separate subcommands.
During handling, confirm that the call came through the necessary subcommand.

```python
context.mute_group(group_id, duration_minutes=30)
context.unmute_group(group_id)
context.is_group_muted(group_id)
context.get_mute_remaining(group_id)
```

`get_mute_remaining()` returns minutes.

### Application services and caller identity

```python
catalog = context.get_command_catalog()
loaded_plugins = context.list_plugins()
is_admin = context.capabilities.is_bot_admin
is_scheduled = context.capabilities.is_system
```

`context.principal` stores the user, group, conversation type, and delivery targets.
`context.capabilities` stores narrow capabilities issued by Core from plugin declarations and current identity.
Use `is_bot_admin` for object-level rules, such as stopping a task as its initiator or a Bot administrator.
Manifest declarations and Core decide command access.

Send proactive OneBot actions with `await context.send_action(action)`.
Management plugins can call `context.reload_config()` or `context.reload_plugins()` under their command permissions.
See [PluginContext](05-api-reference.md#plugincontext) for full signatures and capability properties.

## 💬 Sessions

Create a session:

```python
await context.create_session({"step": "name"}, timeout=300.0)
```

Read a snapshot:

```python
session = await context.get_session()
```

Update atomically:

```python
async def update(working):
    working.set("step", "confirm")
    working.set("value", "小青")


await context.update_session(update)
```

End or query the session:

```python
await context.end_session()
active = await context.has_session()
```

The same session key executes serially.
Group keys contain group ID and user ID. Private keys contain user ID.
A timeout ends the session.

Later operations can call `create_session()` to start a new flow.
Before continuation, Core rechecks access to at least one top-level command from the plugin through the common permission function.
Revoked permission or a prohibited scene closes the session.

## 💬 Message returns

Construct message segments with `core.plugin_base`:

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

QQ displays reply text as plain text. Use plain text in replies.
Use emoji or separate lines for emphasis. Markdown bold, backticks, code fences, and `#` headings remain literal in QQ.
Exported `.md` files and model prompts can use Markdown.

Entry points support these return values:

- String
- OneBot message-segment list
- `DeliverySegments` with a send receipt
- Action list
- `None`, which returns no message

To commit business state after actual delivery, return `DeliverySegments` with a `DeliveryReceipt`:

```python
from core.delivery import DeliveryReceipt, DeliverySegments


def commit() -> None:
    mark_sent(receipt.message_ids)  # IDs for all delivered messages, including each part of a split reply


def unknown() -> None:
    # Handed to the transport without an acknowledgement: it may or may not have been
    # delivered, and message_ids may be empty. Do not treat it as confirmed delivery,
    # and do not roll back to a re-claimable state, or the content may be sent twice.
    mark_delivery_unknown()


receipt = DeliveryReceipt(
    expected_actions = 1,
    commit           = commit,
    rollback         = release_reservation,
    unknown          = unknown,
)
return DeliverySegments(reply, receipt)
```

Core calls `commit` after all physical messages receive acknowledgement.
It calls `rollback` on definite failure and `unknown` on an unknown result.
The plugin decides what an unknown result means: announcements may treat it as done (at-most-once) to avoid duplicates, while reliable deliveries should keep a queryable "unknown" state. State the chosen policy in the help text and README.
Each callback executes at most once.
Proactive delivery uses `send_with_receipt(context.send_action, action, receipt)`.

Core splits long text automatically at message-segment boundaries. You can also use `split_message_segments()`.
Outbound files and remote media follow Core type, size, path, and network budgets.

## 🌐 HTTP, AI, and synchronous libraries

### HTTP

Reuse `context.http_session` for fixed upstream addresses.
Set budgets for response bytes, decompression ratio, MIME, JSON structure, and total time:

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

The application owns and closes the shared session.
Plugins own each request's timeout, redirect, response-size, content-type, and parse-structure budgets.

When chat content controls the target URL, use `core.safe_http.fetch_public_html()` or `fetch_public_bytes()`:

```python
from core.safe_http import fetch_public_html

response = await fetch_public_html(url, timeout_seconds=10)
```

The safe HTTP client checks scheme, DNS, target subnet, and redirects at each hop.
It pins validated addresses. Compression and decompression budgets constrain response bodies.

### External text and images

Validate text and media through shared boundaries before you send third-party, RCON, SSH, or webpage content:

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

`bounded_external_text()` handles ANSI, control characters, character counts, and UTF-8 byte counts centrally.
`validate_image_bytes()` and `validate_image_path()` check actual format, container, dimensions, pixels, frames, and file identity.
Synchronous work, such as image decoding, enters plugin execution budgets through `run_sync()`.

### AI routes

Configure a route at `config.plugins.<plugin_name>.ai.routes.<route_name>`.
Then call it through the current plugin's AI capability:

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

The common AI registry manages providers, models, fallback, retries, and total timeouts.
`pinned_model` supports administrator diagnostics. `list_models()` returns available profiles that satisfy modality requirements.
Plugins receive route results, profiles, attempt counts, and model metadata.

### Synchronous libraries

Submit blocking synchronous calls through `run_sync()`:

```python
from core.plugin_base import run_sync

result = await run_sync(blocking_function, argument)
```

`run_sync()` inherits the current plugin execution gate.
It uses shared workers, per-plugin bulkheads, and a global fair queue.

## 💾 Data persistence

```python
from pathlib import Path

data_file = context.data_dir / "state.json"
data_file.parent.mkdir(parents=True, exist_ok=True)
```

Use these practices for persistent files:

- Explicit schema and version
- Atomic replacement writes
- Bounded file size
- Startup validation
- Backup and recovery paths
- User or group isolation

Core supplies atomic storage, bounded file caches, and path utilities.
Plugins can reuse them according to the data type.

Use atomic helpers for simple JSON state:

```python
from core.plugin_base import load_json, write_json

state = load_json(data_file, default={})
state["count"] = int(state.get("count", 0)) + 1
write_json(data_file, state)
```

## 🧩 Declared services and capabilities

Plugin calls use Core-managed service contracts.
Manifest `services` declares exports. `uses_services` declares consumption.
Core checks service owners, callers, and capabilities.

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

Service fields:

| Field | Description |
|---|---|
| `name` | Stable name in the Core service catalog |
| `callback` | Public asynchronous function name in the entry module |
| `callers` | Calling plugin set specified by the Core service catalog |
| `required_capability` | Additional capability for a specific bridge. ordinary services omit it |

Current services include `voice.synthesize_text`, `chat.reply`, `codex.enqueue_arxiv_summary`, and `core.observe_outgoing_action`.
XiaoQing Agent adds four bridges:

- `xiaoqing_agent` supplies `agent.handle_turn` and `agent.report_delivery` to `xiaoqing_chat`.
  The latter reports delivery of the current reply.
- `codex` supplies `codex.agent_task` to `xiaoqing_agent`.
- `qingssh` supplies `qingssh.agent_command` to `xiaoqing_agent`.

Core checks fixed owners and callers by service name.
It issues `agent.handle_turn` and `command_runner` to user-message contexts.
`command_runner` executes as the initiating user, with each command's normal permission and scene checks.
`codex.agent_task`, `qingssh.agent_command`, and `local_tools` follow `/codex`, `/ssh`, and `/shell` declarations in `plugin.json` respectively.
Each call repeats the permission check.

`codex.agent_task` and `qingssh.agent_command` can start work within the plugin execution lane and return an `asyncio.Task`.
Core awaits the result outside the lane. Caller cancellation also cancels that task.
Serial plugins thus remain available during long commands.

`command_runner` executes commands in a one-time session namespace.
The user's current interactive session remains separate.
New command sessions first pass to plugin `close_session` for cleanup, then Core discards them.
Returned `DeliverySegments` and receipts pass unchanged to the caller for settlement with actual delivery.

`run(..., operation_id=...)` gives each call an independent request ID.
Before commit, receipts collect each physical message ID in `DeliveryReceipt.message_ids`.
Split replies include an ID for each physical message.
The commit callback associates state with actual messages at commit time.

Core capabilities and sessions use the same command permissions.
Secret management follows `/set_secret`. arXiv Codex summaries follow `/codex`.
Session continuation rechecks current access to at least one plugin top-level command.
Revoked permission or a prohibited scene closes the session.
All checks use `core.router.command_access`, the command-execution permission function.

Manifest capabilities:

| Name | Purpose |
|---|---|
| `command_runner` | Execute published commands as the initiator with normal permission and scene checks. `xiaoqing_agent` only |
| `local_tools` | Recheck initiator access to `/shell` before local code execution or file operations. `xiaoqing_agent` only |
| `config_subscription` | Receive configuration revisions for the current plugin |
| `execution_timeout_exempt` | Let the plugin lifecycle manage long-task timeouts |
| `onebot_media` | Query controlled OneBot messages and media |
| `secret_admin` | Administrator secret-command capability |

Core validates capability requests by canonical plugin name.
When you add a bridge or capability, update the schema, protocol, owner, callers, manifest, and authorization tests together.

## 🔄 Error handling

Handle errors at their boundaries:

1. Return clear usage for input-validation errors.
2. Return a plugin-level reduced-service message for external service errors.
3. Log structured context for data-integrity errors and protect the original state.
4. Propagate cancellation through coroutines to the resource owner.
5. Let Core convert entry-point exceptions to public error codes and request IDs.

```python
try:
    payload = await fetch_data()
except TimeoutError:
    context.logger.warning("上游请求超时")
    return segments("服务响应超时，请稍后重试")
```

Use fixed, clear plugin messages for expected input and business errors.
For unexpected exceptions, the common public error exit logs bounded diagnostics and returns a stable error code:

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

Sensitive operations, such as Shell, Jupyter, Codex, and remote management, use `core.sensitive_audit.log_sensitive_operation()`.
Command text, code, paths, and exception text enter `payload` to generate length and HMAC fingerprints.
Logs keep stable operation, status, request ID, job ID, return code, and exception type fields.

## ✅ Documentation and test contracts

Use this order in each plugin README:

1. Function summary
2. Requirements
3. Command entry points
4. Configuration and credentials
5. Data and permissions
6. Operation and troubleshooting
7. Development verification

Manifest `examples` and `invalid_examples` cover each command node.
Plugin tests cover at least these areas:

- Manifest parsing and command catalog
- Normal parameters, boundary parameters, invalid input
- Private chat, group chat, permission scenes
- Session lifecycle
- External service failures and reduced functionality
- Initialization, reload, shutdown
- Data migration and recovery

## 🧭 Next steps

- Public signatures: [API reference](05-api-reference.md)
- Advanced runtime modes: [advanced topics](07-advanced.md)
- Complete plugin index: [plugin manual](09-plugins.md)
