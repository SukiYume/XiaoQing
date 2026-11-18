# XiaoQingBot

[中文](README.md)

<p align="center">
  <img src="docs/assets/xiaoqing-full-body.png" alt="Full-body illustration of XiaoQing, an astronomy sophomore with gray-lavender hair and a blue cardigan, waving while holding an observation notebook" width="300">
  <br>
  <strong>XiaoQing · Escape Institute of Science &amp; Technology</strong>
</p>

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
| Runtime observation | Logs, request IDs, plugin error replies that state the reason and location, and `/metrics` supply runtime information. |

See [Architecture](docs/en/02-architecture.md) and [Message flow](docs/en/08-message-flow.md).

## Getting started

### 1. Prepare the environment

Python `3.11+`, a OneBot v11 implementation, and network access to QQ and applicable external services are necessary.
[NapCatQQ](https://github.com/NapNeko/NapCatQQ) is a supported deployment choice in this guide.

The root `requirements.txt` contains direct dependencies for Core, default plugins, Pendo Web, and the Jupyter kernel.
The arXiv Filter guide describes its optional local-inference dependencies and model assets.

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

Edit the copied `config/config.json` and check these fields:

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
  "logging": {"level": "INFO", "levels": {}, "console": true, "file": true, "keep_days": 5},
  "plugins": {
    "smalltalk_provider": "xiaoqing_chat"
  }
}
```

The example file sets `default_group_ids` to the placeholder `123456789`. Schedules without their own groups send to these groups, so change it to your own group IDs or leave it empty (`[]`).

Edit the copied `config/secrets.json` and set at least these fields:

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

The shortest path is passive Inbound mode, in which OneBot sends events to XiaoQing.
Set this address and these headers in the NapCat HTTP event reporter:

```http
POST /event HTTP/1.1
Host: 127.0.0.1:12000
Authorization: Bearer your-secret-token
Content-Type: application/json
```

The reporter authenticates with the Bearer header, and the token is `inbound_token`. Configuration of the OneBot `secret` signature alone gives HTTP 401.

Also enable the NapCat HTTP API so that it listens on the port in `onebot_http_base` (11001 in the example); replies go out through it.
If NapCat sets an access token, set the same value as top-level `onebot_token` in `secrets.json`. Otherwise keep it as an empty string.

See [Configure OneBot in Getting started](docs/en/01-getting-started.md#️-3-configure-onebot) for active WebSocket mode, authentication details, and the network boundary of public deployments.

### 5. Start and test

```bash
python main.py
```

Configuration is read from `config/` in the checkout, and data and logs go to `data/` and `logs/`. When installed from a wheel, run `xiaoqing`: these three directories live under the current directory, or under the one given with `xiaoqing --root <dir>`.

Startup logs show loaded plugins and OneBot connection status.
In a private chat with XiaoQing, send these commands from a QQ account listed in `admin_user_ids`:

```text
/help
/plugins
/metrics
/help xiaoqing_chat
/help pendo
/xc 你好
/pendo
```

`/xc` needs an AI model and key configured as described in [Getting started](docs/en/01-getting-started.md#ai-plugin-configuration). Without them, it cannot generate replies.

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

| Plugin | Function | Documents |
| --- | --- | --- |
| `xiaoqing_chat` | Persona chat: decides when and whom to answer, reads images and memes, and reviews each reply before delivery. | [User guide (Chinese)](plugins/xiaoqing_chat/README.md), [architecture (Chinese)](plugins/xiaoqing_chat/ARCHITECTURE.md) |
| `xiaoqing_agent` | Calls commands and tools when someone asks XiaoQing to do a task, with the initiating user's permissions. Disabled by default. | [User guide (Chinese)](plugins/xiaoqing_agent/README.md) |
| `xiaoqing_life` | Browses by interest and keeps a diary, supplying material for chat. The life simulation is disabled by default; the pixel observatory site starts and stops with the main program. | [User guide (Chinese)](plugins/xiaoqing_life/README.md), [observatory (Chinese)](plugins/xiaoqing_life/README.md#小青观测站), [architecture (Chinese)](plugins/xiaoqing_life/ARCHITECTURE.md) |
| `xiaoqing_memory` | Unified memory: chat, the agent, and online life write to it, and each night it is consolidated into pages about each person, each group, and herself. | [User guide (Chinese)](plugins/xiaoqing_memory/README.md), [architecture (Chinese)](plugins/xiaoqing_memory/ARCHITECTURE.md), [design (Chinese)](plugins/xiaoqing_memory/DESIGN.md) |
| `pendo` | Events, tasks, notes, diaries, accounts, and reminders, with a Web console and Scriptable widgets. | [User guide (Chinese)](plugins/pendo/README.md), [architecture (Chinese)](plugins/pendo/ARCHITECTURE.md), [Scriptable guide](docs/en/pendo-scriptable-widget.md) |
| `codex` | Manages background sessions and task queues of the local Codex CLI and returns results proactively. | [User guide (Chinese)](plugins/codex/README.md) |
| `arxiv_filter` | Selects the day's arXiv papers with a local interest model and can send them to Codex for summaries. | [User guide (Chinese)](plugins/arxiv_filter/README.md) |
| `flickr` | Browses daily selections, searches, Flickr Commons, and public albums through the official Flickr API. | [User guide (Chinese)](plugins/flickr/README.md) |

The first four plugins together form the runtime of the XiaoQing persona. See [the XiaoQing runtime in Architecture](docs/en/02-architecture.md#-the-xiaoqing-runtime) for the division of work among them.
The [plugin manual](docs/en/09-plugins.md) covers how to enable every built-in plugin, with its commands, settings, and runtime data.

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

Plugin data defaults to `data/<plugin_name>/`. Source, settings, and runtime data use separate directories.
Each `plugin.json` describes entry points, commands, permissions, chat scopes, dependencies, and schedules.

See [Overview](docs/en/00-overview.md) for directories and concepts, and [Plugin development](docs/en/03-plugin-development.md) and [API reference](docs/en/05-api-reference.md) for writing plugins.

## Settings and maintenance

Settings use two files:

- `config/config.json` contains OneBot addresses, command prefixes, logging, AI models, plugin settings, and execution limits.
- `config/secrets.json` contains the Inbound token, administrator QQ IDs, AI provider keys, and external credentials.

If `config.json` is missing or invalid at startup, the process logs the cause and exits with code 1. When ordinary settings become invalid during operation, Core keeps the last known valid snapshot. When secrets are missing, damaged, or inconsistent with the public settings, Core revokes credentials (fail-closed). There are three ways to change settings during operation:

- Change an existing secret: an administrator sends `/set_secret <path> <value>` in private chat.
- An external tool rewrites `secrets.json`: Core privately lists the changed field paths (without values) to administrators, who examine them and send `/reload` to apply.
- Change `config.json` or replace both files: use a service-stop window. A deployment with an independently protected Inbound connection can also send `/reload` through it after the files are written.

See [Configuration live reload](docs/en/06-configuration.md#️-configuration-live-reload) for how each kind of setting takes effect, and [Start the service in Getting started](docs/en/01-getting-started.md#-4-start-the-service) for the protected Windows startup chain and stop entry point.

## Development and tests

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
python scripts/run_full_uat.py --plan-only
python scripts/run_full_uat.py
```

See [Verification and documentation in Plugin development](docs/en/03-plugin-development.md#verification-and-documentation) for how tests are organized and written.
See [Full UAT in Getting started](docs/en/01-getting-started.md#full-uat) for UAT stages, report locations, and external-scenario options.

## Documentation

| Document | Contents |
| --- | --- |
| [Online documentation](https://paris.escape.ac.cn/note/XiaoQing/) | Browser version of the project manual. |
| [Documentation index](docs/en/README.md) | Reading routes and topic index. |
| [Overview](docs/en/00-overview.md) | Functions, concepts, and directories. |
| [Getting started](docs/en/01-getting-started.md) | Installation, settings, connections, and initial verification. |
| [Architecture](docs/en/02-architecture.md) | Components, service boundaries, and data flow. |
| [Plugin development](docs/en/03-plugin-development.md) | Manifests, entry points, sessions, schedules, and tests. |
| [Core modules](docs/en/04-core-modules.md) | Responsibilities of `core/` modules. |
| [API reference](docs/en/05-api-reference.md) | PluginContext, message segments, and public APIs. |
| [Configuration](docs/en/06-configuration.md) | Settings, secrets, deployment, and reloads. |
| [Advanced topics](docs/en/07-advanced.md) | Sessions, schedules, permissions, and Web plugins. |
| [Message flow](docs/en/08-message-flow.md) | Event reception through reply delivery. |
| [Plugins](docs/en/09-plugins.md) | Built-in commands and settings. |
| [Changelog (Chinese)](CHANGELOG.md) | Version changes and upgrade effects. |

## Troubleshooting

When the Bot does not respond, examine these items first:

1. The OneBot and XiaoQing processes are running.
2. The Inbound URL or active WebSocket URI matches the settings on both sides.
3. The reporter's Bearer token equals `inbound_token`, and `onebot_token` is correct for the reply channel.
4. Connection state and request IDs in `logs/xiaoqing.log`. When a reply reads `操作失败：…`, find the matching `public_error` line by the request ID it carries.

See [Troubleshooting in Getting started](docs/en/01-getting-started.md#-troubleshooting) for a process that exits right after start, unresponsive group commands, few ordinary chat replies, port binding, Pendo Web login, failed AI calls, and secret-candidate notifications.

## License

The project uses the [MIT License](LICENSE).
