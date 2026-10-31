# ⚙️ 04 - Core modules

[中文](../04-core-modules.md) | English

This chapter indexes `core/` responsibilities. Maintainers can use runtime symptoms to find the responsible code.
See [architecture](02-architecture.md) for component relationships and the [API reference](05-api-reference.md) for public plugin interfaces.

## ⏰ Application assembly and lifecycle

| Module | Responsibility |
|---|---|
| `app.py` | Stable `XiaoQingApp` facade, component references, public startup entry points |
| `app_support.py` | Pure parsing and utility functions for application assembly |
| `app_config_apply.py` | Serial publication of configuration revisions, secret state, and runtime parameters |
| `app_plugin_context.py` | Issuance of plugin contexts, principals, capabilities, and service views |
| `app_lifecycle.py` | Startup transactions, rollback, task supervision, reverse-order shutdown |
| `app_ingress.py` | Candidate inbound listener binding, commit, old-generation draining |
| `app_delivery.py` | OneBot action delivery, receipts, test-event collection |
| `app_identity.py` | Administrator state, principal authority, identity snapshots |
| `app_scheduling.py` | Plugin schedule publication, replacement, removal |
| `app_plugin_watch.py` | Plugin watcher startup, supervision, backoff, shutdown |
| `lifecycle.py` | Common resource stack, asynchronous cleanup, shutdown budgets |

`XiaoQingApp` coordinates ownership. Corresponding service modules contain business logic.
`app_lifecycle.py` organizes startup and shutdown paths centrally.

## ⌨️ Message dispatch and commands

| Module | Responsibility |
|---|---|
| `message.py` | OneBot segment normalization, text extraction, sender display names, message-property parsing |
| `dispatcher.py` | `MessageContext` construction, A–G processing, session and plugin calls. `run_command_text` executes commands for the agent. |
| `router.py` | Manifest command trees, trigger index, subcommand parsing, conflict detection. `command_access` is the project-wide command permission function. |
| `args.py` | `ParsedArgs` token and option parsing |
| `models.py` | OneBot, manifest, command, service, and configuration data models |
| `auth.py` | Inbound Bearer credential parsing and constant-time comparison |
| `public_errors.py` | Mapping from internal exceptions to public error codes and request IDs |

Dispatcher follows this fixed sequence: processing gates, URL, Bot name, session, command, command fallback, and smalltalk.
Router builds a read-only command catalog from validated manifests. It puts parse results in the plugin context.

## 🧩 Plugin management

| Module | Responsibility |
|---|---|
| `plugin_manager.py` | Plugin management facade and high-level loading operations |
| `plugin_manager_support.py` | Plugin definitions, loading records, manifest support types, utilities |
| `plugin_watcher.py` | Directory discovery, manifest validation, source snapshots, file-change stabilization |
| `plugin_generation.py` | Plugin generation creation, publication, rollback, draining, cleanup |
| `plugin_runtime.py` | Runtime entry points, declared services, call authorization, generation guards |
| `plugin_execution.py` | Per-plugin concurrency, queues, timeouts, circuit breaking, synchronous bulkheads, fair scheduling |
| `plugin_data.py` | Project data directories, layout upgrades, path ownership |

Plugins load in canonical directory-name order.
Watcher builds stable snapshots. Generation owns atomic publication. Runtime owns call boundaries. Execution owns resource budgets.

`plugin_execution.py` implements manifest `concurrency`:

- `parallel` uses the plugin's configured concurrency limit.
- `sequential` permits one plugin entry at a time.

Core validates Python module dependencies before import. It reports command-trigger conflicts before command-catalog publication.

## 🧩 Public plugin capabilities

| Module | Responsibility |
|---|---|
| `context.py` | `PluginContext` implementation, scoped views, convenience methods |
| `interfaces.py` | Protocols, principals, capabilities, interfaces between modules |
| `capabilities.py` | Core-issued capability implementations, including agent command execution, local-tool authorization, and Codex/SSH bridges |
| `plugin_base.py` | Message segments, actions, synchronous tasks, file and split utilities. conversion between `file://` URIs and local paths |

The current plugin name, user, group, request ID, and principal determine the context.
Ordinary plugins receive configuration and secrets for their current namespace.
Manifest capabilities correspond to narrow privileged objects that Core maintains.

## 💬 Sessions, scheduling, and delivery

| Module | Responsibility |
|---|---|
| `session.py` | Session snapshots, session keys, serial locks, timeouts, atomic updates |
| `scheduler.py` | APScheduler assembly, manifest cron registration, target delivery, version-gated bounded shutdown |
| `delivery.py` | In-process send receipts, targeted schedule results, commit after acknowledgement |
| `durable_fanout.py` | Persistent progress and recovery for multi-target notifications |
| `async_keyed_lock.py` | Asynchronous locks that serialize operations by business key |

Session manages user-driven multi-turn interactions.
Scheduler manages time-driven tasks and their broadcast, targeted, or silent delivery modes.
Delivery and Durable Fanout manage business-state commits and proactive message-delivery progress.

## 🌐 OneBot and networking

| Module | Responsibility |
|---|---|
| `onebot.py` | HTTP action sender, active WebSocket client, connection backoff |
| `server.py` | HTTP `/event`, WebSocket `/ws`, `/health`, `/metrics`, inbound scheduling |
| `inbound_policy.py` | Listener addresses, loopback, proxy trust, token policy |
| `safe_http.py` | Security checks for URLs, DNS, target subnets, redirects, outbound requests |
| `bounded_http.py` | Response status, content type, byte budgets, streamed reads |
| `image_validation.py` | Image type, dimensions, pixels, decoding budgets |

Inbound HTTP and WebSocket use the same authentication and session ordering.
For remote content, `safe_http.py` first resolves the target. Then `bounded_http.py` performs a bounded read.

## 🔐 Configuration, AI, and security

| Module | Responsibility |
|---|---|
| `config.py` | JSON reads, source state, read-only snapshots, revisions, file watcher |
| `ai.py` | Providers, model profiles, plugin routes, retries, fallback, total budgets |
| `sensitive_audit.py` | Sensitive-field audit, summaries, redaction |
| `atomic_store.py` | Atomic writes and recovery for JSON and byte data |
| `process_tree.py`, `windows_job.py` | External plugin commands use independent process trees: Windows Job objects or POSIX process groups. Standard output and standard error share one budget. Timeout and cancellation remove the whole tree. `/shell` and XiaoQing Agent share these modules. |
| `bounded_file_cache.py` | Disk cache with TTL, LRU, entry-count limits, byte limits |

Configuration has public and secret parts. The application publishes snapshots by revision.
AI Service resolves providers, models, plugin routes, and credentials from the same revision.

## 📌 Infrastructure

| Module | Responsibility |
|---|---|
| `logging_config.py` | Console, file, color, rotation, log format |
| `metrics.py` | Message, plugin, error, latency, queue metrics |
| `clock.py` | Timezone-aware time and a testable clock |
| `constants.py` | Shared Core boundary constants |
| `exceptions.py` | Domain exception types |
| `version.py` | Runtime version from project metadata or wheel metadata |
| `__init__.py` | Core package boundary |

## 🏗️ Key ownership

| Resource | Creator | Closer |
|---|---|---|
| Shared HTTP session | `XiaoQingApp` | `app_lifecycle.py` |
| Inbound Server | `app_ingress.py` | `app_lifecycle.py` |
| Active WebSocket Client | `app_lifecycle.py` | `app_lifecycle.py` |
| Plugin Generation | `plugin_generation.py` | `plugin_generation.py` |
| Embedded plugin service | Plugin `init()` | Plugin `shutdown()` |
| Scheduler | `app_lifecycle.py` | `app_lifecycle.py` |
| Config Watcher | `app_lifecycle.py` calls `ConfigManager.watch()` | `app_lifecycle.py` |
| Plugin Watcher | `app_plugin_watch.py` | `app_plugin_watch.py` |

The same ownership boundary pairs resource creation with cleanup.
Application shutdown follows the reverse dependency order.

`DeliveryReceipt` tracks all physical actions necessary for one logical reply.
Actions remain pending when they enter the collection queue.
Actual transport success, failure, and unknown results select commit, rollback, and unknown-result handling respectively.
The transport layer counts receipts that Core owns.

Plugin send helpers keep the same settlement authority.
This prevents duplicate success counts for split messages.
After each physical message receives acknowledgement, the transport records its message ID before its result.
The commit callback reads all IDs for the complete reply from `message_ids`.

Standard HTTP reports acknowledge events after actual action delivery completes.

Active WebSocket connection notifications and the read loop run concurrently.
The read loop continues to receive echoes for notification actions.
When the connection ends, Core cleans up notification tasks and pending requests together.

## 🩺 Troubleshooting index

| Symptom | Primary module | Related modules |
|---|---|---|
| OneBot connection and reconnection | `onebot.py` | `app_config_apply.py`, `app_lifecycle.py` |
| Inbound authentication and ports | `server.py` | `inbound_policy.py`, `app_ingress.py` |
| Command matching | `router.py` | `dispatcher.py`, `models.py` |
| Command or session denied by permissions or scene | `router.py` function `command_access` | Corresponding plugin `plugin.json` |
| Missing Core-issued capability | `app_plugin_context.py` | Corresponding command's `plugin.json` |
| Agent command execution | `dispatcher.py` function `run_command_text` | `app_plugin_context.py`, `plugins/xiaoqing_agent/tools.py` |
| Group participation | `dispatcher.py` | Smalltalk Provider plugin |
| Plugin loading and reload | `plugin_watcher.py` | `plugin_generation.py`, `plugin_runtime.py` |
| Plugin timeout and overload | `plugin_execution.py` | `dispatcher.py` |
| Session state | `session.py` | Corresponding plugin session handler |
| Scheduled messages | `scheduler.py` | `durable_fanout.py`, corresponding plugin handler |
| AI route | `ai.py` | `config.py`, `context.py` |
| External URL | `safe_http.py` | `bounded_http.py`, corresponding plugin |

## 🧭 Next steps

- Public interface signatures: [API reference](05-api-reference.md)
- Complete message stages: [message flow](08-message-flow.md)
- Configuration fields: [configuration](06-configuration.md)
