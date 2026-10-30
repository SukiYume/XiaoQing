# 📚 XiaoQingBot documentation

[中文](../README.md) | English

This directory contains the XiaoQingBot project manual. Read the topics in this order: overview, installation, architecture, plugin development, interfaces, configuration, operation, and plugin use.

## 📚 Topic index

| Document | Scope | Readers |
|---|---|---|
| [00 - Overview](00-overview.md) | Purpose, capabilities, concepts, and directory structure | All readers |
| [01 - Getting started](01-getting-started.md) | Installation, configuration, OneBot connection, startup, and initial checks | New users and deployers |
| [02 - Architecture](02-architecture.md) | Runtime components, service boundaries, lifecycle, and data flow | Framework maintainers |
| [03 - Plugin development](03-plugin-development.md) | Manifest, entry points, context, sessions, scheduling, and tests | Plugin developers |
| [04 - Core modules](04-core-modules.md) | Responsibilities and interactions of `core/` modules | Framework maintainers |
| [05 - API reference](05-api-reference.md) | PluginContext, message segments, sessions, and plugin entry point signatures | Plugin developers |
| [06 - Configuration](06-configuration.md) | Configuration fields, secrets, AI routing, network security, and live reload | Deployers and administrators |
| [07 - Advanced topics](07-advanced.md) | Permissions, background tasks, scheduling, service capabilities, performance, and deployment modes | Advanced developers |
| [08 - Message flow](08-message-flow.md) | The complete path from a OneBot event to a reply | Troubleshooters and framework maintainers |
| [09 - Plugin manual](09-plugins.md) | Purpose, commands, configuration, and data for 32 built-in plugins. command and scheduled-task indexes | Users and administrators |
| [Pendo Scriptable widget](pendo-scriptable-widget.md) | Authentication, script, and setup for the iPhone widget | Pendo users |
| [Changelog (Chinese)](../../CHANGELOG.md) | Version changes, affected functions, and verification results | Users and maintainers |

## 📌 Reading sequences

### First startup

1. Read the [overview](00-overview.md) for OneBot, Core, plugins, and data directories.
2. Follow [getting started](01-getting-started.md) to install, configure, connect, and start the service.
3. Use [configuration](06-configuration.md) to set production parameters and secrets.
4. Select a function in the [plugin manual](09-plugins.md), then open its plugin README.

### Plugin development

1. Use [plugin development](03-plugin-development.md) to create a manifest and entry points.
2. Use the [API reference](05-api-reference.md) to find public interfaces.
3. Use [advanced topics](07-advanced.md) to add background tasks, scheduling, and declared services.
4. Read [Core modules](04-core-modules.md) for framework implementation boundaries.

### Message troubleshooting

1. Use [message flow](08-message-flow.md) to identify the current processing stage.
2. Use [architecture](02-architecture.md) to find the component that owns that stage.
3. Check connections, permissions, routing, and execution limits in [configuration](06-configuration.md).
4. Check the plugin README for its trigger conditions and dependencies.

## 🧩 Core plugin documents

| Plugin | User instructions | Architecture |
|---|---|---|
| `xiaoqing_chat` | [README (Chinese)](../../plugins/xiaoqing_chat/README.md) | [ARCHITECTURE (Chinese)](../../plugins/xiaoqing_chat/ARCHITECTURE.md) |
| `xiaoqing_agent` | [README (Chinese)](../../plugins/xiaoqing_agent/README.md) | The README includes module structure, runtime boundaries, and data. |
| `pendo` | [README (Chinese)](../../plugins/pendo/README.md) | [ARCHITECTURE (Chinese)](../../plugins/pendo/ARCHITECTURE.md) |

Each other plugin has a `README.md` in its directory. The [plugin manual](09-plugins.md) supplies a common index.

## 🏗️ Document responsibilities

- `docs/` contains the complete Chinese manual, and `docs/en/` contains the complete English manual. Both use matching plain filenames and language links at the top of each page.
- Chinese documents are the authoritative maintenance source. Update both versions in the same change when editing capabilities, commands, parameters, defaults, permissions, or deployment steps.
- The root README is available in both languages. Plugin guides, acceptance guides, maintenance rules, and the changelog are maintained in Chinese; English manual links to these documents are marked "Chinese".
- The root README supplies the project entry point and the shortest startup procedure.
- Each document in `docs/00-09` owns one topic.
- Plugin READMEs address plugin users and deployers.
- Plugin ARCHITECTURE documents address plugin maintainers.
- Command manifests define runtime help, permissions, scenes, and examples. Declare permissions here. Core evaluates them centrally.
- READMEs describe current capabilities, procedures, and runtime contracts. Put version changes, one-time migrations, and release verification in the changelog.
- The changelog records version changes and verification results.

Code, configuration examples, manifests, and test contracts define the commands, fields, defaults, and paths in these documents.

## 🧭 External references

- [OneBot protocol](https://onebot.dev/)
- [NapCatQQ](https://github.com/NapNeko/NapCatQQ)
- [APScheduler](https://apscheduler.readthedocs.io/)
- [FastAPI](https://fastapi.tiangolo.com/)
- [aiohttp](https://docs.aiohttp.org/)
- [Python asyncio](https://docs.python.org/3/library/asyncio.html)
