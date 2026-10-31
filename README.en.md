# XiaoQingBot

[中文](README.md)

The complete manual is available in [English](docs/en/README.md) and [Chinese](docs/README.md). Plugin guides, maintenance rules, and the changelog are maintained in Chinese; links to these documents are marked "Chinese".

XiaoQingBot is a QQ assistant with plugins. It uses OneBot v11 and Python `asyncio`.

[Python 3.11+](https://python.org) · [MIT License](LICENSE) · [OneBot v11](https://onebot.dev/) · [Online documentation](https://paris.escape.ac.cn/note/XiaoQing/)

```text
QQ / OneBot event → XiaoQing Core → Plugin routing and sessions → OneBot reply
```

## Project purpose

XiaoQingBot supplies a personal QQ assistant for continuous operation.
Core manages OneBot connections, command routing, sessions, schedules, settings, plugin lifecycles, and runtime metrics.
Plugins supply chat, personal records, astronomy tools, remote operations, content parsing, and games.

Two principal plugins supply daily functions:

- `xiaoqing_chat` supports group and private chats, images, emoji, quotations, long-term memory, action planning, and expression learning.
  If `xiaoqing_agent` is enabled, tool execution follows the caller's permissions.
- `pendo` manages events, tasks, notes, diaries, accounts, reminders, searches, and statistics.
  Pendo also supplies a Web console and iPhone Scriptable widgets.

XiaoQing is for private services on loopback addresses, trusted internal networks, or connections protected by a strict reverse proxy.
Plugins are trusted code. They share the Bot's Python process and operating-system permissions.
Install reviewed first-party plugins or other trusted plugins.

## Core functions

| Function | Implementation |
| --- | --- |
| OneBot v11 | Passive HTTP/WebSocket Inbound and an active WebSocket Client. |
| Asynchronous runtime | Core message transport, schedules, HTTP, and plugin entry points use `asyncio`. |
| Plugins | Each plugin has its own manifest, settings namespace, data directory, and lifecycle. |
| Execution control | Bounded queues, per-plugin concurrency control, and fair scheduling govern plugin entry points and synchronous tasks. |
| Command catalog | Manifests supply command hierarchy, aliases, permissions, chat scopes, examples, and help. |
| Permissions | Manifests declare who can use a command and where. Core uses the same policy for commands, sessions, and derived capabilities. |
| Sessions | Core serializes each session key and uses snapshot isolation and transaction commits. Sessions support forms, games, REPL, and SSH. |
| Background tasks | Plugins use proactive-send capabilities to return text, images, and files from long tasks. |
| Scheduled tasks | Manifests declare cron expressions, delivery modes, and target groups. Core schedules, validates, and sends results. |
| Settings | Snapshots, secret protection, file watchers, and runtime reloads manage settings. |
| Runtime observation | Logs, request IDs, plugin error codes, and `/metrics` supply runtime information. |

See [Architecture](docs/en/02-architecture.md) and [Message flow](docs/en/08-message-flow.md).

## First start

### 1. Prepare the environment

Python `3.11+`, a OneBot v11 implementation, and network access to QQ and applicable external services are necessary.
[NapCatQQ](https://github.com/NapNeko/NapCatQQ) is a supported deployment choice in this guide.

The root `requirements.txt` contains direct dependencies for Core, default plugins, and Pendo Web.
The Jupyter and arXiv plugin guides describe their optional kernels and local models.

### 2. Install the project

Run these commands in Git Bash, macOS, or Linux:

```bash
git clone https://github.com/SukiYume/XiaoQing.git
cd XiaoQing
python -m pip install -r requirements.txt
```

### 3. Create the settings

```bash
cp config/config.json.example config/config.json
cp config/secrets.json.example config/secrets.json
```

A minimal `config/config.json` example follows:

```json
{
  "bot_name": "小青",
  "command_prefixes": ["/"],
  "require_bot_name_in_group": true,
  "enable_ws_client": false,
  "enable_inbound_server": true,
  "onebot_http_base": "http://127.0.0.1:11001",
  "inbound_http_base": "http://127.0.0.1:12000",
  "inbound_trusted_tls_proxy": false,
  "timezone": "Asia/Shanghai",
  "log_level": "INFO",
  "plugins": {
    "smalltalk_provider": "xiaoqing_chat"
  }
}
```

A minimal `config/secrets.json` example follows:

```json
{
  "inbound_token": "your-secret-token",
  "admin_user_ids": [123456789]
}
```

Keep tokens, administrator QQ IDs, LLM API keys, and external-service keys in `config/secrets.json`.
Git ignore rules cover the local production settings files.
See [Configuration](docs/en/06-configuration.md) for fields, the AI model registry, and plugin settings.

### 4. Connect OneBot

In passive Inbound mode, OneBot sends events to XiaoQing.
Set this address and these headers in the NapCat HTTP event reporter:

```http
POST /event HTTP/1.1
Host: 127.0.0.1:12000
Authorization: Bearer your-secret-token
Content-Type: application/json
```

The server uses Bearer authentication.
OneBot `secret` and `X-Signature` implement HMAC signatures.
Configuration of that field alone gives HTTP 401.
The reporter must send a custom `Authorization` header. WebSocket is another connection option.

Standard HTTP reports send replies through the OneBot action API at `onebot_http_base`.
The plugin delivery state commits after a successful receipt. The event response is `{}`.
Enable the NapCat HTTP API. Set the matching OneBot credentials in XiaoQing.

A development acceptance client can send `X-XiaoQing-Response-Mode: actions`.
It receives a custom `{"actions": [...]}` body and must execute those actions.
The local command matrix uses this mode.

In active WebSocket mode, XiaoQing connects to OneBot:

```json
{
  "enable_ws_client": true,
  "enable_inbound_server": false,
  "onebot_ws_uri": "ws://127.0.0.1:11000/ws"
}
```

Inbound binds to loopback by default.
For HTTP or WebSocket Inbound, set a nonempty `inbound_token`.
At a public entry point, Nginx or Caddy terminates TLS and forwards requests to loopback.
Controlled networks between containers can use `inbound_trusted_tls_proxy` with a firewall.
See [Configuration](docs/en/06-configuration.md) for the complete Inbound limits.

### 5. Start and test

```bash
python main.py
```

Startup logs show loaded plugins and OneBot connection status.
Send these commands in QQ:

```text
/help
/plugins
/metrics
/help xiaoqing_chat
/help pendo
/xc 你好
/pendo
```

## Command help

Help uses four levels: function area, plugin, subcommand, and command.
This structure keeps each page short for phone screens.

```text
/help
/help pendo
/help pendo todo
/help pendo todo add
/help json pendo
```

| Command | Permission | Function |
| --- | --- | --- |
| `/help [query] [page N]` | Public | Browse or search commands. |
| `/help json [query] [page N]` | Public | Export the command catalog as structured data. |
| `/plugins` | Public | Show loaded plugins. |
| `/reload` | Bot administrator | Reload settings and plugins. Send the result after completion. |
| `/闭嘴 [duration]` | Bot administrator, group | Pause ordinary replies in this group. Default: 10 minutes. Maximum: 24 hours. |
| `/说话` | Bot administrator, group | Resume ordinary replies in this group. |
| `/set_secret <path> <value>` | Bot administrator, private | Change an existing secret path in `secrets.json`. |
| `/get_secret <path>` | Bot administrator, private | Show a redacted value or object keys. |
| `/metrics` | Bot administrator | Show uptime, success rate, errors, and slow plugins. |

The [plugin guide](docs/en/09-plugins.md) lists commands, aliases, permissions, and schedules.

## Principal plugins

### XiaoQing Chat

As the global `smalltalk_provider`, `xiaoqing_chat` observes group and private messages.
Attention gates, rate control, action planning, the main model, and the reply checker select reply content and timing.
The plugin supports text, images, QQ face, NapCat mface, quoted messages, local media, long-term memory, and expression learning.

See the [user guide (Chinese)](plugins/xiaoqing_chat/README.md) and [architecture (Chinese)](plugins/xiaoqing_chat/ARCHITECTURE.md).

### XiaoQing Agent

When a user asks XiaoQing to do a task, `xiaoqing_agent` decides whether tool execution is necessary.
Execution uses the initiating user's permissions.
Existing XiaoQing commands keep their own permissions.
Python, Shell, QingSSH, and Codex follow the declarations for `/shell`, `/ssh`, and `/codex`.
These commands are restricted to Bot administrators.

The plugin replies in XiaoQing's voice after completion.
Long tasks run in the background and send results proactively.
The plugin is disabled by default. `config.plugins.xiaoqing_agent.enabled` enables it.
When it is disabled, `xiaoqing_chat` keeps its usual behavior.
See the [user guide (Chinese)](plugins/xiaoqing_agent/README.md).

### Pendo

Pendo uses one data model for events, tasks, notes, diaries, accounts, and reminders.
Chat commands, the Web console, and Scriptable widgets share one SQLite database.
Browser login codes have a seven-day redemption period.
Redemption creates a seven-day HttpOnly Cookie session.
Scriptable widgets use read-only Bearer tokens with a default lifetime of 365 days.
Both credential types use second-resolution expiry and digest storage.

See the [user guide (Chinese)](plugins/pendo/README.md), [architecture (Chinese)](plugins/pendo/ARCHITECTURE.md),
and [Scriptable guide](docs/en/pendo-scriptable-widget.md).

### Codex and arXiv

`codex` keeps separate background sessions and task queues.
Tasks run in sequence within each tag.
Different tags run concurrently up to `max_parallel_jobs`.
Completed results return through proactive OneBot messages.

`arxiv_filter` sends the current day's selected papers.
Scheduled sends and callers with `/codex` permission can send positive papers to the Codex `astro-ph` session for summaries.
The source-list date and normalized paper-link set identify a summary task.

See [Codex (Chinese)](plugins/codex/README.md) and [arXiv Filter (Chinese)](plugins/arxiv_filter/README.md).

### Flickr

`flickr` uses the official REST API for daily selections, keyword and tag searches, Flickr Commons, public user photos, and public albums.
Search filters include license, order, and date taken.
Replies keep the author, license conditions, and Flickr photo page.
`/flickr more` continues the same query.
See the [Flickr guide (Chinese)](plugins/flickr/README.md).

## Plugin and data directories

```text
XiaoQing/
├── main.py                  # Process entry point
├── config/                  # Examples and local settings
├── core/                    # Core runtime
├── plugins/                 # Plugin source
├── data/                    # Plugin runtime data
├── docs/                    # Project documentation
├── scripts/                 # Startup, synchronization, and UAT scripts
└── tests/                   # Automated tests
```

Plugin data defaults to `data/<plugin_name>/`.
`data_root` sets the project data root.
Source, settings, and runtime data use separate directories.
Deployment scripts can synchronize code and keep production data.

Each `plugin.json` describes entry points, commands, permissions, chat scopes, dependencies, watched files, and schedules.
Plugin names are lowercase ASCII Python identifiers.
Entry points are canonical POSIX `.py` paths relative to the plugin directory.
See [Plugin development](docs/en/03-plugin-development.md) and [API reference](docs/en/05-api-reference.md).

## Settings and maintenance

`config/config.json` contains OneBot addresses, command prefixes, logging, AI models, plugin settings, and execution limits.
`config/secrets.json` contains the Inbound token, administrator QQ IDs, AI provider keys, and external credentials.

The reload command is:

```text
/reload
```

Ordinary settings use the last known valid snapshot.
Secret snapshots use a fail-closed policy.
An active administrator can change an existing secret path through `/set_secret`.
Core saves the file and publishes the new revision in one transaction.

An external tool can save a complete, valid `secrets.json` while `config.json` stays equal to the confirmed version.
The watcher then stages a candidate.
Current administrators, network connections, and plugin credentials continue to use the confirmed snapshot.
Core privately reports file validity and added, removed, or changed field paths to administrators.
The notification contains paths only.
After examination, an administrator sends `/reload` to confirm and apply the candidate.

A service-stop window is necessary for changes to `config.json` or replacement of both sources together.
An independently protected Inbound connection can also execute `/reload`.

The Windows production startup chain is:

```text
scripts/run-bot.vbs → scripts/run-bot-monitor.ps1 → scripts/run_process_with_rotating_logs.py
```

Double-click `scripts/stop-bot.vbs` to stop the monitor, Bot, and NapCat for the same repository.
Processes started by elevated SSH, scheduled tasks, or administrator terminals cause one UAC confirmation at the stop entry point.
After the completion message, double-click `scripts/run-bot.vbs` to start again.
Comments and help in `scripts/` describe synchronization, startup arguments, and production directories.

## Development and tests

| Directory | Test scope |
| --- | --- |
| `tests/core` | Core behavior. |
| `tests/transport` | Protocols and transport. |
| `tests/plugins/<plugin>` | Plugin behavior. |
| `tests/integration` | Component interaction. |
| `tests/tooling` | Test tools and package resources. |

Tests examine input, output, state changes, errors, permissions, concurrency, and resource lifecycles.
Cases cover current features only; tests dedicated to a single fix are not kept.
Parameterized cases combine identical execution paths.
Isolated substitutes supply external-service responses. Web-client tests execute actual JavaScript.

Assertions use runtime results and valid data contracts.
Code review keeps documentation wording, comments, fixed provider domains, removed-file lists, and source implementation form correct.
Tests use URLs to examine parsing, configuration transfer, and access policies.

Common validation commands follow:

```bash
python -m compileall -q core plugins tests
python -m pytest -n 2
python -m ruff check .
python scripts/format_code.py --check
python -m mypy core plugins
git diff --check
```

Complete deployment acceptance uses:

```bash
bash scripts/run_full_uat.sh --plan-only
bash scripts/run_full_uat.sh
```

See [First start](docs/en/01-getting-started.md) for UAT stages, report locations, and external-scenario options.

## Documentation

| Document | Contents |
| --- | --- |
| [Online documentation](https://paris.escape.ac.cn/note/XiaoQing/) | Browser version of the project manual. |
| [Documentation index](docs/en/README.md) | Reading routes and topic index. |
| [Overview](docs/en/00-overview.md) | Functions, concepts, and directories. |
| [First start](docs/en/01-getting-started.md) | Installation, settings, connections, and initial verification. |
| [Architecture](docs/en/02-architecture.md) | Components, service boundaries, and data flow. |
| [Plugin development](docs/en/03-plugin-development.md) | Manifests, entry points, sessions, schedules, and tests. |
| [Core modules](docs/en/04-core-modules.md) | Responsibilities of `core/` modules. |
| [API reference](docs/en/05-api-reference.md) | PluginContext, message segments, and public APIs. |
| [Configuration](docs/en/06-configuration.md) | Settings, secrets, deployment, and reloads. |
| [Advanced topics](docs/en/07-advanced.md) | Sessions, schedules, permissions, and Web plugins. |
| [Message flow](docs/en/08-message-flow.md) | Event reception through reply delivery. |
| [Plugins](docs/en/09-plugins.md) | Built-in commands and settings. |
| [Changelog (Chinese)](CHANGELOG.md) | Version changes and upgrade effects. |

## Fault isolation

### Startup and replies

1. Make sure that the OneBot and XiaoQing processes are running.
2. Compare the Inbound URL or active WebSocket URI with the settings on both sides.
3. Compare the reporter's Bearer token with `inbound_token`.
4. Examine `onebot_token` for the reply channel.
5. Examine connection state, error codes, and request IDs in the logs.
6. After a secret-candidate notification, examine the field paths.
7. Send `/reload` to apply a valid candidate.

The messages `secrets source is inconsistent` and `WebSocket client stopped` identify a secret-source consistency problem.
Examine missing files, damaged files, and simultaneous changes to `config.json`.
For recovery, stop the service. Save complete configuration sources. Then restart to establish a confirmed credential revision.

### Ordinary group replies are absent

1. Make sure that `plugins.smalltalk_provider` names the applicable chat plugin.
2. Examine the group's mute state.
3. Examine attention, rate-control, and action-planning logs in `xiaoqing_chat`.

### The Pendo Web login page keeps appearing

1. Send `/pendo web status` to get the service address.
2. Send `/pendo web token` to get a login code.
3. Redeem the code on the login page.
4. Permit the browser to store cookies.
5. Examine session validation in the Pendo Web log.

### AI plugin calls fail

1. Compare `config.ai.providers`, `config.ai.models`, and the plugin route references.
2. Examine `secrets.ai.providers.<provider>.api_key`.
3. Examine the proxy, network, quota, and provider status.
4. Use the route, profile, and error code in the plugin log to locate the request.

## License

The project uses the [MIT License](LICENSE).
