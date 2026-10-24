# 🧠 07 - Advanced topics

[中文](07-advanced.md) | English

This chapter covers permissions, long tasks, scheduling, declared services, embedded Web services, caches, concurrency, and production operation.
See [plugin development](03-plugin-development.en.md) for basic procedures.

## ⚙️ Private API register

The project registers third-party private APIs centrally.
Each call site has capability probes, exception isolation, stable cleanup paths, and upgrade regression tests.

| Location | Private API | Purpose | Protection |
|---|---|---|---|
| `core/plugin_manager_support.py` | `importlib._bootstrap._get_module_lock` | Reuse CPython module locks during live plugin loading | `getattr` probe, behavior validation, restart-only mode |
| `core/scheduler_compat.py` | APScheduler internal job store, executor, timer, pending future | Scheduler reload and shutdown | Version probe, calling-contract validation, bounded shutdown |
| `plugins/jupyter/jupyter_manager.py` | `_invoke_cleanup` callback reflection and Jupyter cleanup methods | Kernel termination across versions | Restricted signatures, candidate isolation, cleanup reports |
| `plugins/codex/runner.py` | `process._transport.get_pipe_transport` | Subprocess-pipe cleanup | Attribute and type probes, exception isolation, process-tree cleanup |

Dependency upgrades include private-API probe tests and complete shutdown scenarios.

## 🔐 Permission model

One permission system controls who can use each command and in which scene.
Plugin `plugin.json` command nodes declare these rules. Core `core.router.command_access` evaluates them.

| Declaration | Meaning |
|---|---|
| `admin_only: true` | The top-level command is for Bot administrators. Core normalizes this to `permission: bot_admin`. |
| `permission` | `public`, `group_admin`, or `bot_admin`. `group_admin` includes the current group owner, group administrators, and Bot administrators. Children can increase restrictions. |
| `contexts` | `private`, `group`. Children use the intersection with parent scenes. |

These paths use the common decision function:

- Command execution: human messages and agent `command_runner` calls use the same check.
- Session continuation: each later message rechecks current access to at least one top-level command from the session plugin.
  Denied access closes the session.
- Core-issued derived capabilities: secret management follows `/set_secret`. arXiv Codex summaries follow `/codex`.
  Agent local tools, SSH, and Codex follow `/shell`, `/ssh`, and `/codex`. Each call checks current configuration.
- The model-visible agent command list.

Plugin code delegates administrator checks to Core.
Declare branches with different permissions as separate subcommands.
For example, `/mc run` is for Bot administrators. `/mc say` is public.

`/xc reset confirm` is for group administrators in groups. `/xc reset` supports private chat.
Confirm that processing entered through the necessary subcommand.
For example, color writes accept only `add` and `delete` nodes.

Use `context.capabilities.is_bot_admin` for object-level rules, such as stopping a task as its initiator or a Bot administrator.

## 💬 Multi-turn session pattern

Sessions support interactions that wait for the user's next input:

```text
Command entry
  → create_session()
  → later user message
  → handle_session()
  → update_session()
  → end_session()
```

Example:

```python
async def handle(command, args, event, context):
    await context.create_session(
        {"step": "number", "attempts": 0},
        timeout=300.0,
    )
    return segments("请输入一个数字")


async def handle_session(text, event, context, session):
    value = parse_int(text, minimum=1, maximum=100)
    if value is None:
        return segments("请输入 1 到 100 的整数")

    async def commit(working):
        working.set("attempts", working.get("attempts", 0) + 1)

    await context.update_session(commit)
    return segments(f"收到：{value}")
```

A session callback processes one session key.
Coordinate multiple users or groups outside the callback. Commit through a business database or a dedicated lock.
Each continuation repeats the permission check described above.
On revoked permission or a prohibited scene, Core closes the session and calls plugin `close_session` for cleanup.

## 📌 Background-task pattern

Use plugin-owned queues for long tasks:

```text
Command entry
  → validate task
  → write queue and persistent state
  → return job ID

Background worker
  → execute task
  → save artifacts
  → context.send_action()
  → commit completion state
```

Define these boundaries for background queues:

- Per-user, per-label, and global queue capacities
- Concurrent task limits
- Task cancellation and process-tree cleanup
- Restart recovery and idempotency keys
- Output-byte, file, and message budgets
- Plugin-unload draining time
- Proactive send receipts and business-state commit order

Codex and XiaoQing Agent supply complete implementations.
The agent also demonstrates receipt handoff.
Command receipts, such as image reservations, settle when attachments actually send.
Unsent attachments or task completion release pending reservations.

## ⏰ Scheduled tasks

Declare cron in the manifest:

```json
{
  "schedule": [
    {
      "id": "example.daily",
      "handler": "scheduled_daily",
      "cron": {"hour": 8, "minute": 0},
      "delivery": "broadcast",
      "group_ids": [123456789],
      "description": "每日摘要",
      "enabled": true
    }
  ]
}
```

Entry function:

```python
async def scheduled_daily(context):
    return segments("今日摘要")
```

When a task becomes due, Core resolves the schedule delivery mode and target groups.
`broadcast` sends one common result to all target groups.
`targeted` validates and sends `ScheduledDelivery` results. `silent` runs only the task.

`group_ids` follows declaration order. Omitted or `null` values use global `default_group_ids`.
Handlers that manage receipts per target can read the same resolved targets with `context.default_groups()`.

Common CronTrigger fields:

| Schedule | `cron` |
|---|---|
| Daily at 08:00 | `{"hour": 8, "minute": 0}` |
| Every two hours | `{"hour": "*/2"}` |
| Weekdays at 09:00 | `{"day_of_week": "mon-fri", "hour": 9}` |
| First day of each month | `{"day": 1, "hour": 0}` |

Multi-target business notifications can use `durable_fanout.py` to record each target's delivery progress.
Schedule updates and plugin generations share one publication boundary.

## 🧩 Declared services

Core maintains a closed service catalog. Manifests declare owners and callers:

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

Consumer declaration:

```json
{
  "uses_services": ["voice.synthesize_text"]
}
```

Core validates the service name, owner, caller, callback, and necessary capability.
Service calls enter the provider plugin's execution gate. Generation draining protects these calls.

Update these items together when you add a service bridge:

1. `PluginServiceName`
2. Service owner and caller contracts
3. Protocol or capability type
4. Manifest
5. Authorization and lifecycle tests

## 🌐 Embedded Web services

Composite plugins can run FastAPI or other asynchronous services within their own lifecycle:

```python
async def init(context):
    server = await start_web_server(context)
    context.state["web_server"] = server


async def shutdown(context):
    server = context.state.get("web_server")
    if server is not None:
        await server.stop()
```

Recommended internal boundaries:

```text
plugin/
├── main.py              # Core lifecycle adapter
├── config.py            # Plugin configuration parsing
├── services/            # Business services
├── web/
│   ├── server.py        # Web lifecycle
│   ├── api/             # Routes
│   ├── auth.py          # Authentication and sessions
│   └── static/          # Frontend resources
└── data access          # Database and repositories
```

The plugin lifecycle owns the Web server. The database layer owns transactions.
The authentication layer owns credentials and sessions. The routing layer validates requests and formats responses.
Pendo [architecture](../plugins/pendo/ARCHITECTURE.en.md) supplies a complete example.

## 🌐 URL processing extensions

URL Parser receives single-URL messages in Dispatcher stage B.
Its processing sequence follows:

1. Normalize the URL.
2. Validate scheme, DNS, target subnet, and redirects.
3. Set connection, total-time, and response-size budgets.
4. Validate the content type.
5. Extract bounded titles, summaries, and media.
6. Generate OneBot segments.

Remote fetching uses `safe_http.py` and `bounded_http.py`.
Business plugins process response content after these security checks.

## 💬 Smalltalk Provider

`plugins.smalltalk_provider` selects the global smalltalk plugin. The provider implements this entry:

```python
async def handle_smalltalk(text, event, context): ...
```

A provider can combine these stages:

- Attention and trigger decisions
- User, group, and session rate control
- Context and media construction
- Behavior planning
- AI route
- Candidate-reply checks
- Message-segment delivery
- Memory and expression learning

Commands, URLs, sessions, and ordinary chat have separate Dispatcher stages.
The provider handles ordinary-chat candidates.
On explicit invocation, `xiaoqing_chat` passes the turn to `xiaoqing_agent` through `agent.handle_turn` for a decision.
Agent decisions and chat generation run concurrently. Agent takeover discards that chat generation.

## 🔄 Concurrency and backpressure

Core supplies three budget layers:

1. `max_concurrency`: Dispatcher global message concurrency.
2. `plugin_execution`: per-plugin entries, queues, timeouts, and circuit breaking.
3. `run_sync()`: synchronous workers, per-plugin bulkheads, and a global fair queue.

Use bounded task groups for plugin-internal concurrency:

```python
results = await gather_bounded(
    (fetch_one(item) for item in items),
    limit=4,
)
```

Set capacity or byte budgets for background queues, HTTP pools, caches, file reads, and output.
Overload returns a public plugin error with the request ID.

## 💾 Caches and persistent state

### Memory cache

`context.state` supports generation-local caches, task references, and parsed configuration.
A new generation rebuilds these values on reload.

### Disk cache

`BoundedFileCache` supplies:

- TTL
- LRU
- Entry-count limit
- Total-byte limit
- Atomic metadata writes

Cache keys use digests of normalized input.
Business databases and caches have their own directories and recovery policies.

### Database

SQLite plugins should use:

- One database owner
- Explicit transactions
- A schema-version table
- Startup migrations
- `quick_check` and backup verification
- User-scoped query conditions
- Consistent backups of WAL files

## 🔐 Error boundaries

| Boundary | Handling |
|---|---|
| User input | Return explicit fields, ranges, and usage. |
| Third-party HTTP | Classify timeout, status-code, content-type, and response-size errors. |
| AI provider | Route retries, fallback, total budget, model metadata |
| Files and databases | Atomic writes, transaction rollback, integrity checks, backups |
| Background tasks | State machine, cancellation, process-tree cleanup, restart recovery |
| Lifecycle | Creators clean up resources. The application closes in reverse order. |

Plugin logs record business identifiers, stages, upstream states, and request IDs.
User replies use stable public error codes.

## ✅ Debugging and verification

### DEBUG logs

```json
{
  "log_level": "DEBUG",
  "log_to_console": true,
  "log_to_file": true
}
```

### Targeted tests

```bash
python -m pytest tests/plugins/<plugin> -q
python -m ruff check plugins/<plugin> tests/plugins/<plugin>
python -m mypy plugins/<plugin>
```

### Full UAT

```bash
bash scripts/run_full_uat.sh --plan-only
bash scripts/run_full_uat.sh
```

## 🛡️ Production operation

### Windows

```text
scripts/run-bot.vbs
  → scripts/run-bot-monitor.ps1
  → scripts/run_process_with_rotating_logs.py
```

Double-click `scripts/stop-bot.vbs` to stop the monitor, Bot, and NapCat managed by this chain.
Stop mode holds the same repository-level mutex.
It rechecks PID, process name, and absolute command path before process termination.
CIM identity reads use three short retries.
For elevated processes, one UAC-elevated stop instance revalidates and removes the process tree.
After the completion message appears, double-click `scripts/run-bot.vbs` to restart.

Production instances manage existing secret paths with `/set_secret`.
The watcher stages complete, valid external changes to the secrets file alone.
Current administrators check the field-path summary and confirm with `/reload`.
Schedule public configuration changes and complete two-source replacements during shutdown windows.
After file updates, restart the service. Startup pairs public configuration and secrets in a confirmed revision.

### systemd

```ini
[Unit]
Description=XiaoQingBot
After=network-online.target

[Service]
Type=simple
WorkingDirectory=/path/to/XiaoQing
ExecStart=python main.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Replace `WorkingDirectory` with the actual project directory.
The service manager supplies an available `python`, dependencies, and runtime account.
The project entry remains `python main.py`.

### Docker

The repository `Dockerfile` is the container build entry.
Mount `config/`, `data/`, and `logs/` during deployment.
Connect inbound ports to a controlled proxy network.

Production uses a dedicated Bot user, minimum file permissions, explicit network egress, log rotation, data backups, and health checks.

## 🧭 Next steps

- Component ownership: [architecture](02-architecture.en.md)
- Configuration budgets: [configuration](06-configuration.en.md)
- Plugin implementations: [plugin manual](09-plugins.en.md)
