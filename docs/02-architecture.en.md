# 🏗️ 02 - Architecture

[中文](02-architecture.md) | English

This chapter describes XiaoQing Core runtime components, ownership, lifecycle, and main data flows.
See the [API reference](05-api-reference.en.md) for public plugin interfaces and [Core modules](04-core-modules.en.md) for the module index.

## 🏗️ Architecture overview

```text
                           ┌────────────────────┐
                           │ config + secrets   │
                           │ revision snapshot  │
                           └─────────┬──────────┘
                                     │ publish
┌──────────────┐  events   ┌─────────▼──────────┐  actions   ┌──────────────┐
│ OneBot v11   ├──────────►│    XiaoQingApp     ├───────────►│ OneBot v11   │
│ HTTP / WS    │           │   stable facade    │            │ HTTP / WS    │
└──────────────┘           └─────────┬──────────┘            └──────────────┘
                                     │
                    ┌────────────────┼────────────────┐
                    │                │                │
             ┌──────▼──────┐  ┌──────▼──────┐  ┌─────▼───────┐
             │ Dispatcher  │  │PluginManager│  │  Scheduler  │
             │ + Router    │  │ generations │  │ + fanout    │
             └──────┬──────┘  └──────┬──────┘  └─────┬───────┘
                    │                │                │
                    └────────────────┼────────────────┘
                                     │ scoped capabilities
                           ┌─────────▼──────────┐
                           │      Plugins       │
                           │ commands/services  │
                           │ sessions/web/jobs  │
                           └────────────────────┘
```

Core uses one asynchronous process. Plugins are trusted Python extensions. They share process and system permissions.
Plugin execution gates, generation publication, and scoped capabilities manage availability, lifecycle, and interfaces.

## 🧩 Application facade and internal services

`XiaoQingApp` is the stable process-level facade. `core/app.py` assembles components and exposes a small set of public entry points.
The `app_*` modules divide internal service responsibilities:

| Module | Responsibility |
|---|---|
| `app_config_apply.py` | Serial publication of configuration and secret snapshots |
| `app_plugin_context.py` | Issuance of plugin contexts, capabilities, and service views |
| `app_lifecycle.py` | Startup transactions, failure rollback, reverse-order resource cleanup |
| `app_ingress.py` | Candidate inbound listener binding and generation changes |
| `app_delivery.py` | OneBot action delivery, receipts, event collection |
| `app_identity.py` | Administrator state and principal authority |
| `app_scheduling.py` | Publication and removal of plugin schedules |
| `app_plugin_watch.py` | Plugin file-watcher supervision and restart |

These modules share one `XiaoQingApp` instance and process lifecycle.
This division supports unit tests, ownership tracking, and local maintenance.

## ⚙️ Configuration publication

`ConfigManager` reads `config.json` and `secrets.json`. It creates read-only snapshots with revisions.
The application publishes state in one commit sequence:

1. Validate public configuration and secret-source state.
2. Resolve administrators, the inbound token, and the OneBot token.
3. Update plugin configuration views and AI routes.
4. Renegotiate inbound and active WebSocket connections.
5. Publish schedule and plugin runtime parameters.

Public configuration uses the last known good snapshot. The configuration watcher publishes valid public configuration sources.
Secret configuration uses fail-closed snapshots.
Deleted, damaged, or unreadable secrets immediately revoke network credentials and administrator views.
Secrets with an unconfirmed pairing to new public configuration cause the same revocation.
Secret transactions managed by `ConfigManager` write under a lock, then publish the confirmed revision directly.

A complete, valid external change to only `secrets.json` can become a pending candidate.
Public configuration must match the confirmed signature. The runtime generation must remain trusted.
The confirmed snapshot continues to serve requests.

Plugins, administrator views, and network authentication remain isolated from the candidate.
Core privately sends only field-path differences to current administrators.
The `/reload` command reads the files again and publishes the candidate atomically.
Confirm public configuration changes and two-source deployments through a stop/start procedure or independently trusted inbound access.

## 🌐 Network input and output

### Inbound Server

`server.py` supplies these endpoints:

- `POST /event`
- `WebSocket /ws`
- `GET /health`
- `GET /metrics`

HTTP and WebSocket inbound share authentication policy, session keys, admission queues, and ordering rules.
A configuration change first creates a candidate listener. After validation, Core commits the new generation and drains the old generation.

Standard HTTP reports send replies through the OneBot action API. They return `{}` after actual acknowledgement.
Development clients can explicitly select `X-XiaoQing-Response-Mode: actions` to receive a list of actions to execute.
Inbound Bearer tokens and outbound OneBot tokens have separate management.

### Active WebSocket Client

The WebSocket client in `onebot.py` connects to OneBot, receives events, and sends actions.
Connection management uses bounded exponential backoff, jitter, a stability window, and wake-up on configuration changes.

### Outbound delivery

Plugin replies and proactive messages use the application delivery layer:

```text
Plugin message segments
  → Delivery
  → WebSocket Action or HTTP Action
  → send result / receipt
```

Send receipts let plugins commit business state after successful delivery.
When a long reply becomes several physical messages, the receipt commits after all messages receive acknowledgement.
It collects each message ID in order in `message_ids`.
In the commit callback, the plugin associates business records with the actual messages.
Before delivery, Core checks image and file paths, sizes, types, and read budgets.

The event collection queue owns subsequent settlement of collected actions. Successful collection leaves receipts pending.
The WebSocket connection callback and receive loop run concurrently. The receive loop processes echoes that the callback awaits.
When the connection closes, Core cancels and awaits its callback tasks together.

## 💬 Message dispatch

`Dispatcher` converts OneBot events to `MessageContext`. It runs the fixed A–G sequence:

1. Processing gates
2. URL parsing
3. Bot-name response
4. Active session
5. Command
6. Command fallback message
7. Smalltalk Provider

`Router` uses the read-only manifest command catalog to match triggers and parse subcommands.
Before a plugin call, Dispatcher checks permissions and scenes with `command_access`.
This is the project-wide permission decision function.
Session continuation checks, Core-issued capabilities, and the agent's model-visible command list all call this function.
Plugin code delegates administrator checks to Core.
Startup detects trigger conflicts at the same priority and reports errors.

[Message flow](08-message-flow.en.md) describes the input, early-return conditions, and observation points for each stage.

## 🧩 Plugin runtime

### Discovery and loading

`PluginManager` discovers plugins in directory-name order. It performs these steps:

1. Read and validate `plugin.json`.
2. Validate the plugin name, entry path, dependencies, and command conflicts.
3. Build source and resource snapshots.
4. Import an independent generation namespace.
5. Create a plugin-scoped context.
6. Execute `init()`.
7. Publish command, service, and schedule entry points atomically.

### Generation publication

Each load or live reload creates a plugin generation.
After validation and initialization, the candidate becomes the active generation. The retired generation drains its requests.
Resource cleanup follows the reverse creation order.

### File watching

The plugin watcher builds stable snapshots of Python source, plugin-root JSON, and manifest `watch_files`.
The current interpreter must pass the module-lock capability probe before live reload.
In restart-only mode, a process restart applies source changes.

### Execution limits

Each plugin has separate budgets for entry concurrency, queues, timeouts, circuit breaking, and synchronous tasks.
Blocking libraries use `run_sync()` to enter shared workers and a per-plugin bulkhead.
The global scheduler selects tasks fairly between plugins.

Manifest `concurrency` selects the command-entry mode:

- `parallel`: plugin entries run concurrently within configuration limits.
- `sequential`: plugin entries run serially for that plugin.

## 🧩 Plugin context and capabilities

Core issues a narrow context to each plugin. Main capabilities include:

- Public configuration and secret snapshots for the current plugin
- A private data directory and generation-local runtime state
- Logs, metrics, and an HTTP session
- Session API
- Proactive delivery and receipts
- AI routes
- Declared service calls
- Scheduling and mute-state queries

The context expresses permissions through capability objects. Core keeps ownership of sensitive state.
Plugins receive scoped views for their own tasks.
Core issues derived capabilities from the corresponding command's manifest declaration.

Each call checks the current configuration again.
Secret management follows `/set_secret`. arXiv Codex summaries follow `/codex`.
Agent local tools, SSH, and Codex follow `/shell`, `/ssh`, and `/codex`.

## 💬 Session management

`SessionManager` distinguishes interactions with session keys:

- Private chat: user ID
- Group chat: group ID and user ID

Requests for the same key execute serially in admission order.
Handlers read a session snapshot and commit state through an atomic callback.
Exceptions and cancellation keep committed-state consistency.
Before each continuation, Dispatcher calls `command_access` again.
The user must currently have access to at least one top-level command from that plugin in the current scene.
Permission revocation or a prohibited scene closes the session.

Sessions support short, user-driven interactions. Long Codex and XiaoQing Agent tasks use plugin queues and proactive delivery.

## 💾 Scheduling and durable delivery

`SchedulerManager` reads cron expressions, delivery modes, and group targets from manifest `schedule`. It registers handlers by plugin generation.
`app_scheduling.py` implements `broadcast`, `targeted`, and `silent` contracts.
It validates targets centrally and passes messages to the OneBot delivery boundary.
`durable_fanout.py` stores progress for multi-target notifications. After a process restart, delivery resumes from the checkpoint.

Plugin schedule publication belongs to its generation.
During unload or reload, old-generation schedules stop triggering before resource draining starts.

## 🧩 Embedded plugin services

Composite plugins can create internal services in `init()` and close resources in `shutdown()`.
Pendo uses this model for FastAPI, SQLite, frontend resources, reminder scheduling, and chat commands.

`App.shutdown_errors` exposes shutdown results as a read-only snapshot.
The entry point awaits `App.stop()`, then checks the snapshot. Resource-cleanup errors cause a failure exit status.
Runtime acceptance checks the exit status, shutdown-complete log, and port release.
The chat plugin cancels background waits within the shared shutdown budget.
It reserves time for cancellation cleanup and the final disk flush.

Embedded services follow these ownership rules:

- The plugin lifecycle owns service startup and shutdown.
- The plugin data directory owns databases and persistent files.
- Core context supplies logs, configuration, secrets, and delivery capabilities.
- Internal service modules separate the database, authentication, API, tasks, and frontend resources.

## 🤖 XiaoQing Agent collaboration

`xiaoqing_agent` uses the Core service catalog and capabilities for all collaboration with other plugins.

```text
xiaoqing_chat ──agent.handle_turn (decide / act)──► xiaoqing_agent
      ▲                                                  │
      └──────────────── agent.report_delivery ◄─────────┤
                                                         ├─ command_runner ───────► Dispatcher.run_command_text
                                                         ├─ local_tools (follows /shell)
                                                         ├─ qingssh.agent_command ─► qingssh (follows /ssh)
                                                         └─ codex.agent_task ─────► codex (follows /codex)
```

- The chat plugin owns chat history, reply timing, and persona. An explicit invocation passes the turn, persona, and speaking style to the agent.
  Agent decisions and chat generation run concurrently. The agent writes with that persona.
  After takeover, the chat side still sends and records the current reply with its receipt.
- The agent owns tasks, working directories, and task records. Its permissions equal the initiating user's permissions.
  `command_runner` executes only the command stage as that user, in a one-time session namespace.
  It returns command receipts unchanged to the agent. The agent settles them with actual attachment delivery.
  Each call has an independent operation identifier.
- Long Codex and QingSSH execution waits outside each plugin's execution lane. Serial plugins remain available during long commands.

## 🚀 Startup and shutdown order

### Startup

1. Read and validate configuration snapshots.
2. Initialize logging, HTTP, metrics, and session services.
3. Load plugins and publish contexts, services, and the command catalog.
4. Publish plugin schedules.
5. Start inbound and active WebSocket connections.
6. Start configuration and plugin watchers.

### Shutdown

1. Stop new event admission.
2. Stop watchers and schedule triggers.
3. Drain plugin entries and background resources.
4. Close plugins, network connections, and shared services in reverse order.
5. Flush persistent state and logs.

## 🏗️ Data ownership

| Data | Owner | Default location |
|---|---|---|
| Public runtime configuration | ConfigManager | `config/config.json` |
| Secret configuration | ConfigManager | `config/secrets.json` |
| Plugin source | PluginManager | `plugins/<name>/` |
| Plugin runtime data | Corresponding plugin | `data/<name>/` |
| Multi-turn sessions | SessionManager | In-process state |
| Runtime logs | Logging system | `logs/` |
| UAT reports | UAT Runner | `test_reports/` |

Explicit data ownership permits independent code synchronization, production-data retention, backup, and recovery.

## 🧭 Next steps

- Module responsibilities: [Core modules](04-core-modules.en.md)
- Event stages: [message flow](08-message-flow.en.md)
- Plugin contracts: [plugin development](03-plugin-development.en.md)
