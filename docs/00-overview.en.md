# 🧭 00 - Overview

[中文](00-overview.md) | English

This chapter describes the purpose, capabilities, concepts, and directory structure of XiaoQingBot. Continue with [getting started](01-getting-started.en.md) to deploy the service.

## ✨ Project purpose

XiaoQingBot is a QQ assistant with plugins. It uses Python asyncio and OneBot v11. The project has two parts:

- `core/` handles OneBot connections, event parsing, command routing, sessions, scheduling, configuration, plugin lifecycle, execution limits, and runtime metrics.
- `plugins/` supplies chat, personal management, research tools, remote operations, content parsing, external services, and entertainment.

The OneBot implementation connects to the QQ client. XiaoQing processes events and supplies application functions.

```text
QQ ↔ OneBot implementation ↔ XiaoQing Core ↔ plugins
```

You can run the project on a personal computer, home server, NAS, trusted private network, or private server with a reverse proxy.

Python 3.11 or later is necessary. Maintainers use a common code formatter and behavior regression checks. See [code and verification conventions](03-plugin-development.en.md#code-and-verification-conventions).

## ✨ Capabilities

| Area | Plugins | Capabilities |
|---|---|---|
| Core management | `bot_core` | Hierarchical help, plugin list, reload, group mute, secret management, runtime metrics |
| Intelligent chat | `xiaoqing_chat`, `xiaoqing_agent`, `smalltalk`, `chat`, `voice` | Multimodal chat, group participation, task agent, basic answers, TTS |
| Personal management | `pendo` | Events, tasks, notes, journals, accounts, reminders, Web, widgets |
| Astronomy research | `apod`, `arxiv_filter`, `ads_paper`, `astro_tools`, `dict`, `chime` | Astronomy images, paper selection, literature search, calculations, dictionary, FRB data |
| Execution and operations | `codex`, `shell`, `qingssh`, `jupyter`, `minecraft` | Background agent, controlled terminal, SSH, Python REPL, RCON |
| Content and services | `url_parser`, `github`, `earthquake`, `flickr`, `twitter`, `signin`, `adnmb` | Link parsing, trends, alerts, public photography, media extraction, sign-in, community browsing |
| Tools and entertainment | `choice`, `color`, `wolframalpha`, `guess_number`, `qingpet`, `echo` | Random selection, colors, calculations, games, pets, echo |

The project has 31 plugins. The [plugin manual](09-plugins.en.md) describes their commands, configuration, and data. It also contains command and scheduled-task indexes.

## ✨ Core concepts

### OneBot

[OneBot v11](https://onebot.dev/) defines QQ events, message segments, actions, and communication protocols. XiaoQing supports these connections:

- Passive inbound: OneBot sends events to HTTP `/event` or WebSocket `/ws`.
- Active WebSocket: XiaoQing connects to the OneBot WebSocket server.
- Outbound delivery: XiaoQing sends replies through HTTP actions or WebSocket actions.

### Core

Core converts network events to a common message context. It processes gates, URL parsing, name responses, sessions, commands, and smalltalk in sequence.
Core also owns plugin loading, configuration snapshots, scheduled tasks, proactive delivery, and resource cleanup.

### Plugins

A plugin directory contains `plugin.json` and a Python entry point. The manifest declares metadata, command trees, permissions, scenes, scheduled tasks, dependencies, and watched files.
Core loads plugins in a defined order. At startup, it checks command conflicts and dependency declarations.

Plugins use a trusted-code model. They share the Bot Python process and operating-system permissions.
The plugin-scoped context supplies configuration, secrets, data paths, HTTP, AI, sessions, metrics, and message delivery for the current plugin.

### Command catalog

The manifest command tree is the authoritative runtime command catalog. Core generates these items from the same catalog:

- Text `/help`
- JSON command catalog
- Routing rules
- Permission and scene checks
- Tests for valid and invalid examples

The manifest declares who can use each command and in which scene. Core `command_access` evaluates these declarations centrally.
Command execution and each session continuation use this check. Core-issued capabilities also use it.
For example, agent local tools, SSH, and Codex follow `/shell`, `/ssh`, and `/codex` permissions. Plugin code delegates administrator checks to Core.

Help supports hierarchical browsing:

```text
/help
/help pendo
/help pendo todo
/help pendo todo add
```

### Message segments

A OneBot message contains ordered segments. Common types are `text`, `image`, `record`, `face`, `mface`, and `reply`.
Plugins use `core.plugin_base` to construct text, image, audio, and combined replies.

### Sessions and background tasks

Framework sessions support short interactions with successive user inputs. Examples include forms, number guessing, SSH, and Jupyter REPL.
Sessions process each session key in sequence. Snapshot transactions commit session state.

Plugin background queues support long tasks, such as Codex and XiaoQing Agent tasks.
These tasks run independently and use proactive delivery to return results. Delivery-dependent business state commits after a send receipt confirms delivery.

### Configuration and data

- `config/config.json` stores public runtime settings.
- `config/secrets.json` stores administrator identities, tokens, and third-party keys.
- `data/<plugin_name>/` stores plugin runtime data.
- `logs/` stores runtime logs.

Configuration, source code, and runtime data have separate directories. This structure supports deployment, backup, and recovery.

## 🔄 Runtime path

A message follows this high-level path:

```text
OneBot event
  → Inbound Server or active WebSocket Client
  → XiaoQingApp
  → Dispatcher
  → Router / Session / Smalltalk Provider
  → plugin entry point
  → OneBot message segments
  → HTTP or WebSocket Action
```

See [message flow](08-message-flow.en.md) for processing stages and early-return conditions. See [architecture](02-architecture.en.md) for component ownership.

## 💾 Project directories

```text
XiaoQing/
├── main.py                  # Process entry point
├── pyproject.toml           # Project metadata and tool configuration
├── requirements.txt         # Direct dependencies
├── config/                  # Configuration examples and local configuration
├── core/                    # Core runtime
├── plugins/                 # Plugin source code
├── data/                    # Plugin runtime data
├── logs/                    # Runtime logs
├── docs/                    # Project manual
├── scripts/                 # Startup, UAT, synchronization, and maintenance tools
├── tests/                   # Automated tests
└── test_reports/            # Local UAT reports
```

Core responsibilities belong to these module groups:

| Module group | Responsibilities |
|---|---|
| `app.py`, `app_*` | Application facade, configuration publication, capability issuance, network negotiation, scheduling, lifecycle |
| `dispatcher.py`, `router.py` | Message dispatch and command parsing |
| `plugin_manager.py`, `plugin_*` | Plugin discovery, generation publication, execution limits, file watching |
| `session.py`, `scheduler.py` | Multi-turn sessions and scheduled tasks |
| `onebot.py`, `server.py` | OneBot outbound communication and inbound connections |
| `context.py`, `interfaces.py`, `plugin_base.py` | Public plugin capabilities and utility functions |
| `config.py`, `ai.py`, `metrics.py` | Configuration, model routing, runtime metrics |

[Core modules](04-core-modules.en.md) supplies an index for each module.

## 🔐 Design boundaries

### Trusted plugins

Plugins share the Bot process and system permissions. Deployers review plugin code and manage its sources.
Core namespaces, execution gates, and generation management organize capabilities, availability, and lifecycle consistency.

### Bounded resources

Network requests, plugin entry points, synchronous tasks, queues, caches, and shutdown procedures have explicit budgets.
Overload requests follow a fast error path. Resource cleanup follows the reverse ownership order.

### Authoritative definitions

- Manifests define commands, plugin metadata, permissions, and allowed scenes.
- Configuration examples define the public configuration structure.
- `PluginContextProtocol` defines the public plugin context.
- Plugin data directories define runtime-state ownership.
- Test contracts verify the key interfaces in the documentation.

### Platform-independent startup

Start the project with `python main.py` in the current environment. Users prepare Python and dependencies. Project scripts call tools from `PATH`.

## 🧭 Next step

Follow [getting started](01-getting-started.en.md) to install, configure, and start the service.
