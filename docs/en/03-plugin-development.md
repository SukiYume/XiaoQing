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

Repository-wide checks and code conventions: see [code and verification conventions](#-code-and-verification-conventions).

## 📏 Code and verification conventions

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
Tests cover the behavior contracts of current features; a business fix adds or adjusts cases for the affected feature instead of keeping a test dedicated to one fix.
Test data uses temporary directories or databases.

Tests are organized by current feature: `tests/core` covers Core behavior, `tests/transport` covers protocols and transport, `tests/plugins/<plugin>` covers plugins, `tests/integration` covers component interaction, and `tests/tooling` covers test tools and package resources.
Cases examine input, output, state changes, errors, permissions, concurrency, and resource lifecycles, and parameterized cases combine identical execution paths.
Cases enter through public entry points: commands, services, scheduled tasks, and a module's public functions.
Substitutes sit only at boundaries: network and external services, the model gateway, message sending, the clock, randomness, subprocesses, process-wide logging setup, the operating system, and third-party libraries.
Functions, constants, and module state of Core and plugins are not replaced, with these exceptions:
to stop concurrent work at one exact point, a test wraps that function in a barrier that calls the original once released;
to check failure handling at an exact point no real input reaches, a test injects the failure there;
module-level locks, caches, and background-task handles are reset between tests;
constants that point at fixed file locations are redirected to temporary directories;
timeouts and polling intervals are shortened so tests do not wait in real time;
a limit that would take very large input to reach is lowered;
a flag that records whether an optional dependency is available is set to unavailable to check behavior without it.
Web-client tests execute actual JavaScript.
Substitutes and helpers shared by several test files live in `tests/helpers/`; test files do not import from each other.
Assertions use runtime results and valid data contracts.
Code review keeps the exact wording of prompts, persona, help text, and constants correct, along with documentation wording, comments, fixed provider domains, removed-file lists, and source implementation form.
Sample URLs in tests examine parsing, configuration transfer, and access policies.

The duplicate-code check runs with the tests (`tests/tooling/test_check_redundancy.py`; run it alone with `python -m pytest tests/tooling/test_check_redundancy.py`): `core`, `plugins`, `scripts`, and `main.py` must not contain two functions with identical bodies (functions under 8 lines and interface declarations containing only `...`, `pass`, or `raise NotImplementedError` are exempt). When it fails, keep one copy and call it from the others; a copy that must stay separate is listed in that test file's `ALLOWED` with its reason.

READMEs describe current use and configuration.
ARCHITECTURE documents describe module responsibilities, data flow, and ownership.
The project manual describes public contracts.
Keep commands, permissions, and aliases consistent with `plugin.json`, and link to shared conventions between modules.

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
| `name` | string | Required | Canonical plugin name, equal to its directory name |
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
| `name` | string | Required | Python module name that Core can locate without importing it. It must not start with `plugins` |
| `required` | boolean | `true` | Controls plugin loading when the module is missing |
| `description` | string / `null` | `null` | Dependency purpose |

`dependencies` describes Python import prerequisites.
Plugins collaborate through `services` and `uses_services`.
The loading transaction checks owners, callers, and service availability together.

### Command fields

Top-level command object:

| Field | Type | Default | Description |
|---|---|---|---|
| `name` | string | Required | Stable command name used in the command code |
| `triggers` | string[] | Required | Nonempty, unique top-level triggers. Matching is case-sensitive |
| `help` | string | Required | Function summary |
| `usage` | string | `/<first trigger>` | Complete usage |
| `admin_only` | boolean | `false` | Top-level Bot administrator shortcut. `true` normalizes to `bot_admin` |
| `permission` | `public` / `bot_admin` / `group_admin` | `public` | Permission level |
| `contexts` | Array of `private` / `group` | Both | Allowed scenes |
| `priority` | integer | `0` | Top-level routing priority. Larger values match first |
| `examples` | string[] | `[]` | Valid examples, maximum 16 |
| `invalid_examples` | string[] | `[]` | Invalid argument, scene, or permission examples, maximum 16 |
| `effect` | `read` / `write` | Undeclared | Whether the command only reads or also adds, changes, or deletes data. See below |
| `subcommands` | array | `[]` | Subcommand nodes |

Subcommand node:

| Field | Type | Default | Description |
|---|---|---|---|
| `name` | string | Required | Canonical name, unique among siblings |
| `aliases` | string[] | `[]` | Aliases, unique among siblings |
| `help` | string | Required | Function summary |
| `usage` | string | Required | Complete usage |
| `match` | `prefix` / `exact` | `prefix` | `prefix` keeps following business arguments. `exact` accepts only the complete node path. |
| `permission` | Permission enum | Parent value | A child can set stricter permissions. |
| `contexts` | Scene array | Parent value | A child can restrict scenes. |
| `examples` | string[] | `[]` | Valid examples |
| `invalid_examples` | string[] | `[]` | Invalid examples |
| `effect` | `read` / `write` | Undeclared | Not inherited from the parent |
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

Core generates stable code `example.todo.add` for this leaf node, made of plugin name `example`, command name `todo`, and subcommand name `add`.
The plugin gets the parsed node and remaining arguments through `context.command_invocation`.

#### Read/write effect

`effect` states whether the command changes data: `read` only queries, `write` adds, changes, or deletes. Callers such as the Agent treat every plugin the same way from this declaration, without plugin-specific code.
Within one task, XiaoQing Agent does not run an identical `write` command a second time. Read commands repeat normally.
Declare `write` honestly even for commands whose identical text has a different effect each run, such as stepwise undo; the agent asks the initiator before repeating them.

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
| `handler` | string | Required | Public asynchronous function name in the entry module |
| `cron` | object | Required | APScheduler `CronTrigger` arguments |
| `id` | string / `null` | `null` | Task ID, unique within the plugin. Defaults to `handler` when omitted |
| `delivery` | `broadcast` / `targeted` / `silent` | `broadcast` | Delivery mode that Core executes |
| `group_ids` | positive integer[] / `null` | `null` | Target groups. Omission uses `default_group_ids` |
| `description` | string / `null` | `null` | Task description |
| `enabled` | boolean | `true` | Task enabled state |

Scheduler validates handlers, cron expressions, task IDs, delivery modes, and targets when it publishes a plugin.
When a task becomes due, Core applies its delivery mode:

- `broadcast` sends the handler's returned segments to every target group for that schedule.
- `targeted` accepts `ScheduledDelivery` messages. It checks group targets against the schedule group list and supports controlled private targets.
- `silent` executes the handler with zero delivery targets. Core ignores its return payload and rejects proactive delivery from that schedule context.

`broadcast` and `targeted` take target groups from the first source that sets them: deployment config `config.plugins.<plugin>.schedules.<schedule ID>.group_ids`, the entry's `group_ids`, then global `default_group_ids`.
The schedule ID is the entry's `id`, or its `handler` when `id` is omitted. An empty list in the deployment config sends the schedule to no group.
Targets keep the order of `group_ids`. Core reads them on every run, so a config change applies after `/reload`.
`plugin.json` ships with the repository and holds no real group IDs; deployment-specific targets belong in the deployment config.
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
Use `strict=True` when the command protocol requires complete quotes.
`parse_int()` accepts ASCII decimal integers. It returns a range-checked `int` or `None`.
See [argument parsing](05-api-reference.md#-argument-parsing) for the complete return structure.

### `handle_session()`

Core calls this entry when the current session key has an active session:

```python
async def handle_session(text, event, context, session): ...
```

The handler reads the supplied snapshot. It commits persistent changes through `context.update_session()`.

### `close_session()`

Core calls this entry when it ends a session. The plugin releases connections, background tasks, and other resources here. Without it, the session simply ends:

```python
async def close_session(event, context, session): ...
```

Core calls it in these cases:

- The user sends an exit word.
- The user can no longer use any top-level command of the plugin.
- `handle_session()` raises an exception.
- The command runner discards the one-time session that a command opened.

An exception raised here is only logged, and the session still ends.

### `handle_smalltalk()`

Core calls this entry when the plugin is the `smalltalk_provider`:

```python
async def handle_smalltalk(text, event, context): ...
```

### `observe_message()`

A `smalltalk_provider` can supply this entry. Core calls it after the processing gates and before the URL stage so that the provider observes every message that is not skipped. The return value is ignored:

```python
async def observe_message(clean_text, event, context): ...
```

URLs, commands, command-prefixed text, and active-session input are skipped and are not observed.

### `handle_url()`

A plugin named `url_parser` can supply this entry:

```python
async def handle_url(url, event, context): ...
```

### `call_bot_name_only()`

The current `smalltalk_provider` can handle messages that contain only the Bot name:

```python
async def call_bot_name_only(context):
    return segments("我在")
```

A Bot-name-only message (only the Bot name, or only an `@` of the bot without a picture) usually checks whether the bot is online, so it must be fast: Core calls this entry of the current `smalltalk_provider` instead of `handle_smalltalk()`. `xiaoqing_chat` picks a random line from `bot_name_only_replies`, records it in the chat history once sent, and treats the same person's next message within a minute as addressed to her.

### Entry selection order

After message cleaning and group gating, Dispatcher selects an entry from the message form. While a group is muted, it skips `handle_url()` and `handle_smalltalk()`:

1. A URL-only message calls `handle_url()`.
2. A Bot-name-only message calls `call_bot_name_only()`.
3. An active session calls its plugin's `handle_session()`. A `None` result continues to command routing.
4. A command-catalog match calls `handle()`.
5. Ordinary text calls the current `smalltalk_provider` entry `handle_smalltalk()`.

Core returns a common message when a message starts with a command prefix, the first character after the prefix is a letter, and no command matches.
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

Plugins log through a standard-library logger and `core.log.LogEvent`. `PluginContext` has no logger:

```python
import logging

from core.log import LogEvent

log = logging.getLogger(__name__)

log.info(LogEvent("reply.done", chat_id=chat_id, elapsed_s=1.23, reply=text))
log.warning(LogEvent("fetch.failed", source="baike", error=exc))
log.info(LogEvent("cache.cleared"))
```

The logger name is the module name, such as `plugins.<plugin>.<module>`. The `levels` map in the [logging settings](06-configuration.md#️-logging-settings) sets levels by this name or by its prefix `plugins.<plugin>`.
`LogEvent(name, **fields)` redacts and renders at construction. The message part of the line is the event name followed by the fields, for example `reply.done chat_id=123 elapsed_s=1.23 reply="<17ch #5948c5c6>"`.
Fields appear in the order given. A value that contains a space, a quote, or `=` is JSON-quoted. An event without fields prints only its name.
The object keeps only the redacted result, so `str()`, `repr()`, and the fallback output after a formatting failure never see the original values.

Event names are lowercase ASCII, dot-separated as `<area>.<action>[.<result>]`, for example `reply.done`, `fetch.failed`, or `proactive.skip`.
Event names carry no plugin prefix; the logger name on the line already shows the source.

Field values are rendered by the first matching row:

| Value | Output |
|---|---|
| `None` | Field omitted |
| Bool | `true` / `false` |
| Number: int, float, and other numeric types (`IntEnum`, `Decimal`, `Fraction`, numpy scalars) | A plain number, for example `count=3` or `ratio=0.25` |
| `date`, `datetime`, `time` | ISO text, for example `day=2026-11-17` |
| Exception instance or `core.log.ExceptionSummary` | Several fields for type, location, status, and so on; see below |
| `dict` | Flattened, with keys written as `parent.child` |
| List, tuple, set | `[N items]` |
| Bytes | Length and fingerprint |
| String under a credential key or a body key | Length and fingerprint |
| String under any other key that fully matches `[A-Za-z0-9_.:/@+-]{1,128}` | As is |
| Any other string (with spaces, Chinese, control characters, or more than 128 characters) and other objects (`Path`, custom objects) | Length and fingerprint; a `Path` has the same fingerprint as `str(path)` |

Keys are judged as whole strings, flattened keys included:

- Credential key: after splitting on `_` and `.`, any part is `token`, `secret`, `password`, `passwd`, `cookie`, `auth`, `authorization`, `credential`, `apikey`, or `key`.
- Body key: split on `.` into segments; if any segment is a body segment, the whole key is a body key, so every item flattened under a body key is redacted (`payload.file_name` follows `payload`). A body segment is exactly `name`, `nickname`, `sender_name`, `user_name`, `display_name`, or `card`; or it does not end in `_id`, `_code`, `_count`, `_kind`, `_type`, `_status`, or `_name`, and after splitting on `_` any part is `text`, `content`, `message`, `msg`, `history`, `dialogue`, `reply`, `prompt`, `response`, `raw`, `output`, `input`, `thinking`, `reason`, `goal`, `hint`, `description`, `marker`, `term`, `emotion`, `summary`, `evidence`, `path`, `url`, `uri`, `host`, `payload`, `command`, `cmd`, `query`, `title`, `preview`, `body`, or `detail`. So `reason_code`, `command_name`, and `message_type` are ordinary keys.

A redacted value reads `<17ch #5948c5c6>` (text of 17 characters) or `<10B #5948c5c6>` (10 bytes). It and `[N items]` contain a space, so the log line JSON-quotes them, while `fields` holds them unquoted.
The fingerprint is the first 8 hex digits of an HMAC under a process-random key. Within one process the same value has the same fingerprint, so lines can be correlated; the key changes on restart, so fingerprints do not correlate across restarts.

An exception is written as the following fields (shown for the key `error`); its message is not written:

- `error`: the type name.
- `error.at`: the location `file:line`. It is the deepest frame of plugin code (`plugins/`) in the traceback; without plugin frames, the deepest frame of other project code (`core/`, `scripts/`, `main.py`); otherwise the deepest real file. It is omitted when the exception has no traceback.
- `error.cause`: the type name of the innermost exception in the `__cause__` / `__context__` chain. It is omitted when there is no chain or the chain is suppressed (`raise … from None`).
- Status-like attributes, present only when the exception carries them: `error.status` (HTTP status, also read from `response.status_code` or `response.status`), `error.errno`, `error.winerror`, `error.returncode`, `error.lineno`, `error.colno`, `error.sqlite` (SQLite error name), and `error.module` (the module that failed to import).

For example: `fetch.failed source=baike error=AIRequestError error.at=plugins/hello/main.py:31 error.cause=ConnectionResetError error.status=504`.
`core.log.summarize_exception(exc)` returns the same content as an `ExceptionSummary`, which renders as the same fields when passed as a field value.

Write fields with these rules in mind:

- Pass QQ IDs, group IDs, `chat_id`, `message_id`, `job_id`, and other identifiers directly. They stay readable.
- Use space-free ASCII values for labels such as `status=ok`, `route=chat`, or `plugin=pendo`. They stay readable.
- Put free text such as message bodies, replies, prompts, commands, paths, URLs, host names, and nicknames under body keys. Only length and fingerprint remain.
- Write program-generated reasons as `reason_code=`. `reason` is a body key, so its value is redacted.
- Write exceptions as `error=exc`. The type, location, innermost type of the exception chain, and status-like attributes remain; exception messages stay out of the log.
- Keep tokens, cookies, and authorization headers out of logs. When correlation is needed, put them under a credential key.

Core sets the request ID; plugins do not write it in messages or fields.
Core creates the ID when a message comes in (for active WebSocket when the client receives the event, for inbound HTTP once per request, for inbound WebSocket once per frame), and sets one for each command run through `command_runner` and each scheduled-task run.
Core's receipt line, plugin handling, reply sends, and handling failures all carry the same ID in the request-ID column. `asyncio.create_task`, `asyncio.to_thread`, and `run_sync()` copy the context, so background work keeps the ID of the request that started it.
Core calls plugin lifecycle hooks (`init`, `shutdown`) in a scope without a request ID.

A long-running loop starts during one request and then serves many requests, such as a worker that runs queued jobs.
It clears the starting request's ID with `log_scope(None)` at its entry and opens a scope with each unit's recorded request ID while handling that unit:

```python
from core.log import log_scope


async def _worker(self) -> None:
    with log_scope(None):
        while job := await self._next_job():
            with log_scope(job.request_id):
                await self._run(job)
```

Core's inbound scheduler workers and the active WebSocket's per-sender queue drainers work the same way: they carry no ID of their own and handle each event under that event's ID.

Level conventions:

| Level | Use |
|---|---|
| `DEBUG` | Troubleshooting detail: raw frames, transport detail, optional diagnostics |
| `INFO` | Something finished or changed state: message received, command done, scheduled run finished, connection established; xiaoqing_chat step logs |
| `WARNING` | A handled degradation: fallback, upstream timeout or network error, bad data skipped, missing configuration that disables a feature |
| `ERROR` | Needs a person: corrupted data, invalid configuration, failed cleanup, a truly unexpected exception (the public error exit). Network and upstream failures do not count. |

Log one line per event at one call site.

Tests read logs with `caplog`.
`logged_events(caplog, name)` in `tests/helpers/assertions.py` returns events in record order, and assertions read the redacted values in `fields`. Tests that need a real log file use the `logged_lines` fixture in `tests/helpers/app_test_support.py`.

```python
import logging

from tests.helpers.assertions import logged_events


async def test_timeout_is_logged(caplog):
    caplog.set_level(logging.WARNING, logger="plugins.hello")
    ...
    (failed,) = logged_events(caplog, "fetch.failed")
    assert failed.fields["error"] == "TimeoutError"
```

Core measures plugin-entry call counts, durations, errors, and cancellations centrally.

### Generation-local state

`context.state` is an in-memory dictionary shared within the current plugin generation.
Use it for caches and task registries. Write persistent state to `context.data_dir`.

### Permissions and mute state

`plugin.json` declares command users and scenes through `admin_only`, `permission`, and `contexts`.
Core checks them before plugin calls with `core.router.command_access`, and plugin code does not decide administrator access itself.
Declare branches with different permissions, such as administrator-only writes, as separate subcommands. During handling, confirm that the call came through that subcommand.
See the [permission model](07-advanced.md#-permission-model) for the check paths and examples.

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
Use `is_bot_admin` only for object-level rules. Manifest declarations and Core decide command access; see the [permission model](07-advanced.md#-permission-model).

Send proactive OneBot actions with `await context.send_action(action)`.
Management plugins can call `context.reload_config()` or `context.reload_plugins()` under their command permissions.
See [PluginContext](05-api-reference.md#-plugincontext) for full signatures and capability properties.

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
When the user sends `退出`, `取消`, `exit`, `quit`, or `q`, Core calls `close_session()` and then ends the session. That message does not reach `handle_session()`.
Before continuation, Core rechecks access to at least one top-level command from the plugin as described in the [permission model](07-advanced.md#-permission-model).
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

The same module holds a few small helpers shared by plugins:

| Name | Purpose |
|---|---|
| `BROWSER_USER_AGENT` | The User-Agent to send when a request must look like a browser |
| `validated_proxy_url(value, *, allow_path=False)` | Validates a configured proxy URL. It returns a valid value unchanged and raises `ValueError` otherwise; the error never contains credentials |
| `split_https_url_on_hosts(url, hosts)` | Returns the split URL when it is an https URL on one of the listed hosts with no userinfo and the default port, and `None` otherwise. It inspects the spelling only and performs no DNS lookup |

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
| `required_capability` | Additional capability for a specific bridge. Ordinary services omit it |

Current services include `voice.synthesize_text`, `chat.reply`, `codex.enqueue_arxiv_summary`, and `core.observe_outgoing_action`.
XiaoQing Agent adds four bridges:

- `xiaoqing_agent` supplies `agent.handle_turn` and `agent.report_delivery` to `xiaoqing_chat`.
  The latter reports delivery of the current reply.
- `codex` supplies `codex.agent_task` to `xiaoqing_agent`.
- `qingssh` supplies `qingssh.agent_command` to `xiaoqing_agent`.

XiaoQing Life adds three bridges:

- `xiaoqing_life` supplies `life.turn_context` and `life.export_memes` to `xiaoqing_chat`.
  The data concerns only XiaoQing herself, so calls are not gated by user identity, and system principals may use them.
- `codex` supplies `codex.travel_postcards` to `xiaoqing_life` to draw the observatory's daily travel postcards.
  It is used through `capabilities.travel_postcards` and issued only to the scheduled task's system principal.

XiaoQing Memory adds four bridges, all supplied by `xiaoqing_memory` and used through `capabilities.memory`:

- `memory.record` is called by `xiaoqing_chat`, `xiaoqing_agent`, and `xiaoqing_life`.
- `memory.recall` is called by `xiaoqing_chat` and `xiaoqing_agent`.
- `memory.search` and `memory.forget` are called by `xiaoqing_chat`.

Core writes the caller name, the principal kind, and the user principal's chat into each payload.
Fields of the same name supplied by the caller are ignored. System principals may use these services.

Core checks fixed owners and callers by service name.
It issues `agent.handle_turn` and `command_runner` to user-message contexts.
`command_runner` executes as the initiating user, with each command's normal permission and scene checks.
`codex.agent_task`, `qingssh.agent_command`, and `local_tools` follow `/codex`, `/ssh`, and `/shell` declarations in `plugin.json` respectively.
Each call repeats the permission check.
`onebot_actions` follows the `/qq` declaration and is issued to `xiaoqing_chat`, with the same check on each call.
It uses the OneBot request channel that returns responses, outside outgoing-message observation and receipts. Sending messages still uses `context.send_action`.

`codex.agent_task`, `codex.travel_postcards`, and `qingssh.agent_command` can start work within the plugin execution lane and return an `asyncio.Task`.
Core awaits the result outside the lane. Caller cancellation also cancels that task.
Serial plugins thus remain available during long commands.

`command_runner` executes commands in a one-time session namespace.
The user's current interactive session remains separate.
New command sessions first pass to plugin `close_session` for cleanup, then Core discards them.
Returned `DeliverySegments` and receipts pass unchanged to the caller for settlement with actual delivery.
`run(..., request_texts=...)` passes the initiator's own words in this agent session, oldest first (Core keeps the latest 16, each cut to 4000 characters). The command reads them from `context.request_texts`. Use them only when necessary, for example when the portion count for a physical feeder must match what the person said. Ordinary writes follow their arguments, as when a user sends the command directly, and do not compare literally against the original words. `context.request_texts` is empty when a user sends the command directly, and an ordinary message event cannot supply it.

`run(..., operation_id=...)` gives each call an independent request ID.
Before commit, receipts collect each physical message ID in `DeliveryReceipt.message_ids`.
Split replies include an ID for each physical message.
The commit callback associates state with actual messages at commit time.

Core-derived capabilities (secret management, QQ operations, arXiv Codex summaries, agent local tools, SSH, and Codex) and session continuation use the same command permissions.
All checks use `core.router.command_access`, the command-execution permission function; see the [permission model](07-advanced.md#-permission-model).

Manifest capabilities:

| Name | Purpose | Plugins that can request it |
|---|---|---|
| `command_runner` | Execute published commands as the initiator with normal permission and scene checks | `xiaoqing_agent` |
| `local_tools` | Recheck initiator access to `/shell` before local code execution or file operations | `xiaoqing_agent` |
| `config_subscription` | Receive configuration revisions for the current plugin | `pendo` |
| `execution_timeout_exempt` | Let the plugin lifecycle manage long-task timeouts | `codex`, `jupyter`, `qingssh`, `shell` |
| `inbound_backlog` | List messages from the current event's sender in this chat that have arrived but are not being handled yet | `xiaoqing_chat` |
| `onebot_actions` | Let a Bot administrator call OneBot actions through `/qq` and read the responses | `xiaoqing_chat` |
| `onebot_media` | Query controlled OneBot messages and media | `xiaoqing_chat` |
| `secret_admin` | Administrator secret-command capability | `bot_core` |

Core validates capability requests by canonical plugin name.
See [declared services](07-advanced.md#-declared-services) for what to update together when you add a bridge or capability.

## 🔄 Error handling

Handle errors at their boundaries:

1. Return clear usage for input-validation errors.
2. Return a plugin-level reduced-service message for external service errors.
3. Log structured context for data-integrity errors and protect the original state.
4. Propagate cancellation through coroutines to the resource owner.
5. Let Core turn unexpected exceptions raised by an entry point into public error replies.

```python
try:
    payload = await fetch_data()
except TimeoutError as exc:
    log.warning(LogEvent("fetch.failed", source="upstream", error=exc))
    return segments("服务响应超时，请稍后重试")
```

Use fixed, clear plugin messages for expected input and business errors.
Hand unexpected exceptions to the common public error exit, which logs one diagnostic line and replies with one line of explanation:

```python
from core.public_errors import public_error_response

try:
    return await perform_operation()
except Exception as exc:
    return public_error_response(context, exc, component="hello.handle")
```

The reply reads `操作失败：<reason>（<exception type> @ <location>；request_id：…）`, for example:

```text
操作失败：请求超时，请稍后重试（TimeoutError @ plugins/hello/main.py:4；request_id：3f9a1c2e）
操作失败：上游服务返回 HTTP 404，重试无效（HttpStatusError @ plugins/hello/main.py:8；request_id：3f9a1c2e）
操作失败：插件内部错误，重试无效，请联系管理员（KeyError @ plugins/hello/main.py:16；request_id：3f9a1c2e）
```

The reason comes from the first classifiable exception in the chain, outermost first, and the exception type and location come from that same link. The location follows the rule of the `error.at` log field; without a traceback only the type is shown.
`SafeHttpError` and `ResponseTransportError` only relay a lower error, so they give way to a classifiable exception deeper in the chain.
An exception takes the first matching category in this table:

| Category (log `category`) | Exceptions | Reason in the reply |
|---|---|---|
| `model` | The AI gateway's `AIError` | Depends on `AIRequestError.category`, for example model timeout, rate limit, authentication failure, or model unavailable, each saying whether a retry helps; `AIConfigError` means the model configuration is invalid |
| `timeout` | `TimeoutError`, `requests` `Timeout`, `subprocess.TimeoutExpired` | Request timed out; retry later |
| `bad_response` | JSON parse failures, bounded-HTTP response format, size, and redirect errors, `SafeHttpError`, aiohttp `ContentTypeError`, `requests` JSON, decoding, and redirect errors | The upstream data cannot be parsed; a retry later may help |
| `upstream_status` | `HttpStatusError`, aiohttp `ClientResponseError`, `requests` and `urllib` `HTTPError` | The upstream service returned HTTP <status>; 408, 425, 429, and 5xx say retry later, other statuses say a retry will not help |
| `network` | `ConnectionError`, `ResponseTransportError`, aiohttp `ClientError`, `requests` `RequestException`, `URLError`, DNS and SSL errors, paramiko connection errors | Network connection failed; retry later |
| `dependency` | `ImportError` | Missing dependency <module>; contact the administrator |
| `database` | `sqlite3.Error` | With `SQLITE_BUSY` or `SQLITE_LOCKED` the database is busy, retry later; otherwise the database operation failed, contact the administrator |
| `io` | Other `OSError`, `subprocess.SubprocessError` | File or system operation failed; contact the administrator |
| `internal` | Any other exception | Internal plugin error; a retry will not help, contact the administrator |

The replies are in Chinese; the table paraphrases them.
`public_error_response()` and `public_error_message()` both log one ERROR-level `public_error` on the `plugins.<plugin>` logger, with `component`, `category`, `reason_code` (`AIRequestError.category`) for model errors, and `error=exc`, followed by the traceback. The request ID matches the reply.
The log's `error` is the exception passed in, expanded by the rules in [logs and metrics](#logs-and-metrics); the reply's type and location come from the deciding link, so the two can differ.
The exception message reaches neither the reply nor the log.
When a failure should not disturb the user (background work, silent degradation), `log_public_error()` writes only this log line and sends no reply.

Sensitive operations, such as Shell, Jupyter, Codex, and remote management, also log with `LogEvent`, for example `log.info(LogEvent("execute", status="started", payload=command))`.
Command text, code, and paths go in `payload`, which keeps only length and fingerprint. `status`, `job_id`, and return codes stay readable. Exceptions go in `error=exc`, which keeps the type, location, and status-like attributes, at ERROR level.

## ✅ Documentation and test contracts

See [verification and documentation](#verification-and-documentation) for what each kind of document covers.
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
