# 🛠️ 06 - Configuration

[中文](../06-configuration.md) | English

This chapter describes Core public settings, secrets, the AI registry, plugin namespaces, network boundaries, and live-reload semantics.
Plugin READMEs own plugin-specific fields.

## ⚙️ Configuration files

| File | Content | Management |
|---|---|---|
| `config/config.json` | Public runtime settings, model profiles, plugin configuration | Copy `.example`, then edit for the deployment environment. |
| `config/secrets.json` | Tokens, administrator identities, AI keys, plugin credentials | Keep on the deployment host with restricted read permissions. |
| `config/config.json.example` | Current public configuration structure | Maintain with source versions. |
| `config/secrets.json.example` | Current secret configuration structure | Maintain with source versions. |

Create local configuration:

```bash
cp config/config.json.example config/config.json
cp config/secrets.json.example config/secrets.json
```

Core combines both files into read-only settings snapshots with revisions.
Plugins read their namespaces through scoped contexts.
Top-level `ai` in `config.json` holds the common model registry. Top-level `plugins` holds public plugin namespaces.
The matching `ai` and `plugins` objects in `secrets.json` hold credentials.

## ⚙️ Core settings

### Bot and commands

| Field | Type | Example | Description |
|---|---|---|---|
| `bot_name` | string | `"小青"` | Unique runtime Bot name for invocation, group gates, chat persona, history role labels, and default connection notifications |
| `command_prefixes` | string[] | `["/"]` | Command prefixes |
| `require_bot_name_in_group` | boolean | `true` | Bot-name gate for ordinary group text |
| `default_group_ids` | positive integer[] | `[123456789]` | Group targets for `broadcast` / `targeted` schedules with omitted `group_ids` |
| `timezone` | IANA timezone | `"Asia/Shanghai"` | Timezone for Scheduler, `context.now()`, and business dates |

`command_prefixes` supports multiple prefixes.
Manifest `triggers` stores command words. Router handles prefixes during message parsing.
`/reload` publishes these five settings in a new revision and triggers background plugin reload.

### OneBot communication

| Field | Type | Example and range | Description |
|---|---|---|---|
| `enable_ws_client` | boolean | `false` | Active OneBot WebSocket Client switch |
| `onebot_ws_uri` | absolute URL | `ws://127.0.0.1:11000/ws` | Active WebSocket address, with `ws` or `wss` |
| `onebot_http_base` | absolute URL | `http://127.0.0.1:11001` | OneBot HTTP action address, with `http` or `https` |
| `ws_queue_size` | integer | `200`, `1..10000` | Active WS event queue and inbound waiting backlog |
| `enable_inbound_server` | boolean | `true` | HTTP/WS Inbound Server switch |
| `inbound_http_base` | plaintext listener URL | `http://127.0.0.1:12000` | HTTP `/event` listener base. Requires explicit port and root path. |
| `inbound_ws_uri` | plaintext listener URL | `ws://127.0.0.1:12000/ws` | WebSocket listener address. Requires explicit port. |
| `inbound_ws_max_workers` | integer | `8`, `1..128` | Inbound event workers and WS fan-out concurrency limit |
| `inbound_ws_broadcast_timeout_seconds` | number | `5.0`, `0 < value <= 300` | Budget for one WebSocket broadcast |
| `inbound_trusted_tls_proxy` | boolean | `false` | Permit plaintext listeners on controlled proxy networks |

When HTTP or WebSocket inbound is enabled, top-level `inbound_token` in `secrets.json` must be nonempty.
HTTP and WebSocket share this token, admission queue, and session ordering.

HTTP reporters authenticate with `Authorization: Bearer <inbound_token>`.
Standard reports send replies through the action API at `onebot_http_base`. They return `{}` after successful actual delivery.
A reachable reply channel and its `onebot_token` are necessary for HTTP-report deployments.

Development acceptance clients can explicitly send `X-XiaoQing-Response-Mode: actions` to obtain action lists.
The caller owns action execution in this mode.
OneBot `secret` / `X-Signature` specifies HMAC signatures. The current inbound interface validates Bearer tokens.

Active WebSocket and HTTP actions use top-level `onebot_token` in `secrets.json` for Bearer authentication.
An empty string in a valid secrets snapshot selects the anonymous OneBot connection mode agreed by both services.

Inbound admission capacity is `inbound_ws_max_workers + ws_queue_size`.
Each private user and each group/user pair executes serially in admission order.
Different session keys share workers for parallel processing.
At capacity, HTTP returns an overload status. WebSocket returns an overload response.

### Inbound network boundary

Default listener addresses use loopback. Public access follows this path:

```text
Public HTTPS/WSS
  → Nginx / Caddy TLS termination and access control
  → loopback HTTP/WS
  → XiaoQing Inbound
```

Controlled networks between containers can set `inbound_trusted_tls_proxy: true`.
Use a firewall to restrict listener traffic to proxy sources.

`inbound_http_base` uses `http://`. `inbound_ws_uri` uses `ws://`.
XiaoQing listeners handle plaintext HTTP/WS. An outer proxy or network layer supplies TLS termination.
For a non-loopback host, also set `inbound_trusted_tls_proxy: true` and restrict host firewall access to proxy sources.

### Runtime and data

| Field | Type | Default and range | Activation | Description |
|---|---|---|---|---|
| `max_concurrency` | integer | `5`, `1..1024` | Live reload | Dispatcher global active-message limit |
| `data_root` | path string | `"data"` | Restart | Plugin runtime-data root |
| `enable_plugin_watcher` | boolean | `false` | Live reload | Plugin file-watcher switch |
| `enable_plugin_schedules` | boolean | `true` | Reload | Master switch for plugin schedules; isolated runs (such as the full UAT) set it to `false` so passing a schedule time never calls external accounts or delivers messages |
| `plugin_poll_interval` | number | `3600`, `0.01..86400` | Live reload | Plugin file-polling interval in seconds |
| `session_timeout` | number | `300`, `0 < value <= 604800` | Live reload | Default session idle timeout in seconds |
| `timezone` | IANA timezone | `"Asia/Shanghai"` | Live reload | Scheduler and business-clock timezone |

`data_root` accepts project-relative paths or absolute host paths. Keep it outside the `plugins/` source tree.
Each plugin receives `data_root/<plugin_name>/`.
When Core first finds `plugins/<name>/data/`, it atomically migrates the contents to the new data root.
It archives the source directory at `data_root/.legacy-plugin-data/<name>/`.
Restart the main process after a `data_root` change.

The plugin watcher runs an interpreter capability probe at startup.
Passing environments support live reload. Restart-only environments apply source changes through process restart.
The watcher monitors plugin Python, root JSON, and manifest `watch_files`.
It publishes new generations after complete stable-snapshot validation.

### Windows production startup

| Field | Example | Constraint | Description |
|---|---|---|---|
| `napcat_account` | `"1234567890"` | Empty string or 5–20 decimal digits | NapCat startup positional argument |
| `mkl_threading_layer` | `"TBB"` | Empty string or at most 64 letters, digits, periods, underscores, and hyphens | Bot subprocess `MKL_THREADING_LAYER` |

`scripts/run-bot-monitor.ps1` reads these fields before subprocess creation.
The startup chain calls Python from the current `PATH`.
The deployment environment keeps ownership of the interpreter and dependencies.
Field changes apply at the next monitor-chain startup.

`scripts/stop-bot.vbs` is the Windows double-click stop entry point.
It calls monitor `-Stop` mode. A repository-level mutex prevents new instances during shutdown.
It identifies process trees with current repository script paths and the NapCat executable path.

CIM command-line reads use three bounded retries.
When elevated SSH, scheduled tasks, or administrator terminals create processes, the ordinary desktop entry can show one UAC prompt.
The elevated stop instance rechecks PID, process name, and absolute command path.
After shutdown completes, double-click `scripts/run-bot.vbs` to restart.

## ⚙️ Plugin execution settings

`plugin_execution` manages asynchronous entries and blocking synchronous tasks:

```json
{
  "plugin_execution": {
    "timeout_seconds": 60,
    "parallel_limit": 4,
    "admission_queue_limit": 64,
    "sync_parallel_limit": 1,
    "sync_queue_limit": 16,
    "failure_threshold": 3,
    "cooldown_seconds": 60,
    "drain_timeout_seconds": 5,
    "global_sync_queue_limit": 256,
    "overrides": {
      "codex": {"timeout_seconds": 0},
      "shell": {"timeout_seconds": 0}
    }
  }
}
```

| Field | Range | Description |
|---|---|---|
| `timeout_seconds` | `0` or `0.1..86400` | One plugin-entry budget. `0` assigns control to the plugin lifecycle. |
| `parallel_limit` | `1..1024` | Entry concurrency for `parallel` plugins |
| `admission_queue_limit` | `0..10000` | Plugin-entry waiting queue |
| `sync_parallel_limit` | `1..3` | Synchronous worker concurrency per plugin |
| `sync_queue_limit` | `0..10000` | Synchronous waiting queue per plugin |
| `failure_threshold` | `1..10000` | Circuit-breaker failure threshold |
| `cooldown_seconds` | `0.1..86400` | Circuit-breaker cooldown |
| `drain_timeout_seconds` | `0.1..3600` | Unload draining budget |
| `global_sync_queue_limit` | `1..100000` | Global synchronous waiting queue |
| `overrides` | object | Per-plugin overrides of these policies |

Manifest `concurrency: sequential` fixes entry concurrency at 1. `parallel` uses `parallel_limit`.
Synchronous libraries enter synchronous budgets through `run_sync()`.

`admission_queue_limit` and `sync_queue_limit` count waiting slots. `0` permits zero waiting slots.
Total plugin-entry admission capacity equals active slots plus waiting slots.
`overrides.<plugin_name>` can supply any policy subset.

Omitted fields inherit global policy.
JSON `null` causes configuration-validation failure.
At `failure_threshold`, the execution gate enters `cooldown_seconds` cooldown.
Plugin reload waits up to `drain_timeout_seconds` for current-generation cleanup.

## ⚙️ Logging settings

| Field | Type | Example | Description |
|---|---|---|---|
| `log_level` | string | `"INFO"` | Root level. Common values: `DEBUG`, `INFO`, `WARNING`, `ERROR`. |
| `log_to_file` | boolean | `true` | Write `logs/xiaoqing.log` and `logs/xiaoqing_error.log`. |
| `log_to_console` | boolean | `true` | Console output |
| `log_use_color` | boolean | `true` | ANSI colors in interactive terminals |
| `log_max_size_mb` | number | `10` | Main and error log per-file threshold in `size` mode |
| `log_backup_count` | integer | `5` | Kept rotated-file count |
| `log_rotation` | `time` / `size` | `"time"` | Main log rotation at midnight or by size |

The error log records `ERROR` and higher levels and rotates by size.
Plugin loggers automatically attach plugin names and request IDs.
Core and plugins use summaries or redacted values for tokens, cookies, authorization headers, and keys.
The process creates log handlers during startup. Restart the main process after changes to these fields.

## 🧠 AI/VLM registry

AI configuration has two parts, with credentials kept separately:

1. **Models** (`config.ai`): `providers` holds service addresses and `models` gives each model you use a name.
2. **Who uses which model** (`config.plugins.<name>.ai.routes`): each plugin route lists model names. The first is the primary model and the rest are ordered fallbacks.
3. `secrets.ai.providers`: provider API keys.

Sampling temperature, output budget, timeouts and retries belong to the task itself, so each plugin declares defaults in code and the main configuration can leave them out. To change one, write that route as an object and override the field.

```json
{
  "ai": {
    "providers": {
      "openai":   {"api_base": "https://api.openai.com/v1", "max_tokens_field": "max_completion_tokens"},
      "deepseek": {"api_base": "https://api.deepseek.com"}
    },
    "models": {
      "gpt-terra":      {"provider": "openai", "model": "gpt-6.1-terra", "modalities": ["text", "image"], "request_defaults": {"reasoning_effort": "none"}},
      "deepseek-flash": {"provider": "deepseek", "model": "deepseek-flash", "modalities": ["text"], "request_defaults": {"thinking": {"type": "disabled"}}}
    }
  },
  "plugins": {
    "xiaoqing_chat": {
      "ai": {
        "routes": {
          "chat":    ["gpt-terra", "deepseek-flash"],
          "checker": ["gpt-terra", "deepseek-flash"]
        }
      }
    }
  }
}
```

### Provider

| Field | Type | Default | Rule |
|---|---|---|---|
| `api_base` | absolute HTTP(S) URL | required | Includes scheme and host; omits credentials, query and fragment |
| `endpoint_path` | absolute URL path | `/chat/completions` | Starts with `/`; omits query and fragment |
| `proxy` | absolute HTTP(S) URL or empty string | `""` | Optional proxy for this provider |
| `max_tokens_field` | `max_tokens` or `max_completion_tokens` | `max_tokens` | Field name this API uses for the output budget. Current OpenAI models accept only `max_completion_tokens`; routes always say `max_tokens` and Core sends the field named here |

### Model

| Field | Type | Rule |
|---|---|---|
| `provider` | string | Name from `config.ai.providers` |
| `model` | string | Model name sent to the upstream API |
| `modalities` | string[] | Non-empty set drawn from `text`, `image`, `audio` |
| `request_defaults` | object | Extra fields sent with every request to this model, such as disabling thinking. Core reserves `model`, `messages`, `stream`, sampling parameters and tool fields |
| `fixed_sampling` | bool, default `false` | Set to `true` for a model with fixed sampling that rejects `temperature` / `top_p`; Core then omits both |

Define each upstream model once. Give it two names only when you need two fixed modes, such as one with thinking and one without.

### Plugin route

A route value is a model name, a list of model names, or an object:

```json
{
  "routes": {
    "triage":  "gpt-terra",
    "chat":    ["gpt-terra", "deepseek-flash", "glm-5.2"],
    "checker": {"models": ["gpt-terra", "deepseek-flash"], "timeout_seconds": 20}
  }
}
```

The object form accepts the fields below. Values resolve in this order: what the route says in the main configuration, then the default declared by the plugin, then the Core default in this table.

| Field | Core default and range | Description |
|---|---|---|
| `models` | required; one name or a non-empty list | Primary model followed by ordered fallbacks |
| `temperature` | optional, `0..2` | Route sampling temperature |
| `top_p` | optional, `0..1` | Route nucleus sampling |
| `max_tokens` | optional, `1..1000000` | Route output token limit |
| `timeout_seconds` | `30`, `0.1..600` | Budget for one upstream request |
| `total_timeout_seconds` | `max(timeout, 60)`, `0.1..1800` | Budget for all retries and fallbacks |
| `max_retry` | `1`, `0..10` | Retries per model |
| `retry_interval_seconds` | `0.5`, `0..60` | Exponential backoff base |
| `fallback_on` | Core default error set | Error categories that trigger the next model |
| `request_defaults` | `{}` | Additional route request fields |

Defaults declared by the bundled plugins (`declare_route_defaults`, in each plugin's code):

| Plugin route | Purpose | temperature / top_p | max_tokens | Request / total timeout (s) | Retries |
|---|---|---|---|---|---|
| `xiaoqing_chat.chat` | Replies | 0.8 / 0.6 | 768 | 15 / 60 | 1 |
| `xiaoqing_chat.reasoning` | Planning, memory retrieval, summaries, expression learning, life-record extraction | 0.6 / 0.9 | 2048 | 15 / 45 | 1 |
| `xiaoqing_chat.checker` | Reply review | 0 / 0.8 | 8192 | 12 / 30 | 0 |
| `xiaoqing_chat.vision` | Image reading | 0.2 / 0.9 | 800 | 20 / 60 | 1 |
| `xiaoqing_agent.agent` | Task execution | 0.2 / — | — | 180 / 600 | 1 |
| `xiaoqing_agent.triage` | Triage | 0.1 / — | — | 15 / 20 | 1 |
| `xiaoqing_agent.vision` | `read_image` on local images | 0.2 / — | 800 | 20 / 60 | 1 |
| `xiaoqing_life.reasoning` | Digest, research, wind-down | 0.6 / 0.9 | 2048 | 30 / 120 | 1 |
| `xiaoqing_life.vision` | Meme image check | 0.2 / 0.9 | 800 | 20 / 60 | 1 |
| `pendo.parse` | Natural-language parsing | 0.3 / — | 1000 | 30 / 60 | 1 |
| `xiaoai.plan` | Mi Home action planning | 0 / — | 1600 | 25 / 28 | 0 |
| `ads_paper.summary` | Paper summaries | 0.7 / — | 1200 | 60 / 90 | 0 |

### Where AI parameters come from

Provider connections live in `config.ai.providers`, model definitions in `config.ai.models`, credentials in `secrets.ai.providers`, and the models each route uses in `config.plugins.<plugin>.ai.routes.<route>`. Plugins declare defaults for sampling, output budget, timeouts and retries; override them in the same route when needed. Optional `model_aliases` give `/xc model` short names; without them the model names are used directly. `/xc model default` selects the first available model in `routes.chat`.

XiaoQing Chat loads a typed snapshot of these routes. Replies use `chat`; planning, memory retrieval, summarization and expression learning use `reasoning`; reply review uses `checker`; image analysis uses `vision`. Agent execution uses `agent`, task triage uses `triage` (falling back to `agent` when it is not configured), and `read_image` uses `vision`. Routine calls inherit route settings. Explicit long-form requests can increase output and time budgets; a truncated image analysis can increase its output budget. Optional work and whole Agent tasks remain bound by their own business limits.

`plugins/xiaoqing_chat/config/xiaoqing_config.json` manages persona, memory and participation behavior. Sampling values, token limits, foreground/background request timeouts and retries, planner request timeouts, checker budgets and vision request settings belong to the corresponding routes. Deep-chat mode uses chat-route sampling, and route order defines the default model. The behavior file rejects these AI fields, `default_model_alias` and a nested `ai` registry with an error that points to the main configuration.

DEBUG logs record resolved routes, models and request settings. These records contain parameter metadata; keys and message content remain private.

### Provider secret

```json
{
  "ai": {
    "providers": {
      "deepseek": {
        "api_key": "replace-with-provider-key"
      }
    }
  }
}
```

Each provider stores its API key at `secrets.ai.providers.<provider_name>.api_key`.
Model `request_defaults` enter the request first. Route defaults override them next.
Call parameters finally override sampling and budget fields.

AI Service first calls the route's first available profile.
It applies bounded retries and ordered fallback for transport, timeout, rate-limit, server, model-unavailable, invalid-response, and empty-response errors.
`fallback_on` can also include `request_timeout` and `conflict`.
The calling plugin handles authentication, permission, request-parameter, and configuration errors.
`total_timeout_seconds` covers the complete model chain.

Missing provider credentials, absent `api_key`, empty strings and whitespace-only keys make that model unavailable. Core skips unavailable entries in order before sending requests, and model lists show available entries. Pinning a model with a missing key or using a route with no usable keys produces an explicit configuration error. Malformed URLs, unknown model references and invalid key types remain validation errors. Upstream authentication failures retain their existing error behavior.


## ⚙️ Plugin configuration namespaces

Public plugin settings:

```text
config.plugins.<plugin_name>
```

Secret plugin settings:

```text
secrets.plugins.<plugin_name>
```

Plugins read them through these interfaces:

```python
context.get_config("path.to.value")
context.get_secret("path.to.value")
context.get_settings_snapshot()
```

### Core plugin selection

| Path | Example | Description |
|---|---|---|
| `plugins.smalltalk_provider` | `"xiaoqing_chat"` | Dispatcher global smalltalk provider: `smalltalk` or `xiaoqing_chat` |

### Chat and Pendo

| Path | Example | Description |
|---|---|---|
| `plugins.chat.daily_user_limit` | `20` | Successful calls per user per day, `1..1000000` |
| `plugins.chat.daily_global_limit` | `100` | Successful calls globally per day, `1..1000000` |
| `plugins.pendo.web_enabled` | `true` | Pendo Web lifecycle switch |
| `plugins.pendo.web_host` | `"127.0.0.1"` | Web listener host |
| `plugins.pendo.web_port` | `12001` | Web listener port, `1..65535` |
| `plugins.pendo.web_session_cookie_secure` | `false` | Use `true` for HTTPS to add Secure to browser session cookies. |
| `plugins.pendo.web_demo_enabled` | `false` | Web demo-space switch |
| `plugins.pendo.ai.routes.parse` | list of model names | Models for natural-language structured parsing |

Pendo publishes its five Web fields atomically by configuration revision.
Listener address changes rebuild the plugin-owned Web server.
Login codes and browser sessions have 7-day lifetimes. Widget tokens have 365-day lifetimes.
See [Pendo Web (Chinese)](../../plugins/pendo/README.md) for authentication, demo mode, and data directories.

### ADS Paper and XiaoQing Chat AI

| Path | Description |
|---|---|
| `plugins.ads_paper.ai.routes.summary` | ADS paper AI-summary route |
| `plugins.xiaoqing_chat.ai.routes.chat` | Main reply route |
| `plugins.xiaoqing_chat.ai.routes.checker` | Reply-checking route |
| `plugins.xiaoqing_chat.ai.routes.reasoning` | Planning and reasoning route |
| `plugins.xiaoqing_chat.ai.routes.vision` | Image-understanding route. Models must include `image` modality. |
| `plugins.xiaoqing_chat.ai.model_aliases` | Optional short names for `/xc model`, mapped to model names |

Each of the four XiaoQing Chat routes names its own model order. The plugin declares defaults for sampling, request timeout, total timeout and retries; see the table under "Plugin route" above.
Behavior, participation, memory, and expression parameters are in `plugins/xiaoqing_chat/config/xiaoqing_config.json`.
They can also appear under `config.plugins.xiaoqing_chat`. File values take priority during merging.
See [XiaoQing Chat configuration (Chinese)](../../plugins/xiaoqing_chat/README.md) for field groups.

### XiaoQing Agent

The task agent handles requests that ask XiaoQing to act.
All users can use it within their own permissions. The default state is disabled.

| Path | Default | Description |
|---|---|---|
| `plugins.xiaoqing_agent.enabled` | `false` | Main switch. When disabled, `xiaoqing_chat` replies normally, as with an absent agent. Changes apply to the next message. |
| `plugins.xiaoqing_agent.ai.routes.agent` | Necessary | Execution route. The model must support `tools` calls. |
| `plugins.xiaoqing_agent.ai.routes.vision` | Image reading is unavailable without it | Route `read_image` uses for local images. Models must include `image` modality. |
| `plugins.xiaoqing_agent.ai.routes.triage` | Reuse `agent` | Chat-or-task decision route. A fast model with thinking disabled is recommended. |
| `plugins.xiaoqing_agent.inline_wait_seconds` | `12` | Current-turn wait in seconds. Longer tasks send an initial reply and deliver results in the background. |
| `plugins.xiaoqing_agent.task_timeout_seconds` | `1800` | Total task duration |
| `plugins.xiaoqing_agent.max_steps` | `40` | Model turns per task |
| `plugins.xiaoqing_agent.max_admin_tasks` | `3` | Concurrent Bot-administrator tasks |
| `plugins.xiaoqing_agent.max_member_tasks` | `4` | Total concurrent other-user tasks. `0` limits use to Bot administrators. |
| `plugins.xiaoqing_agent.max_tasks_per_member` | `1` | Concurrent tasks per other user |

The agent has the same command access as the initiator.
Commands enforce their own permissions.
Python, Shell, and file operations follow `/shell`. SSH follows `/ssh`.

Codex follows `/codex`.
`plugin.json` limits these three commands to Bot administrators.
See [XiaoQing Agent (Chinese)](../../plugins/xiaoqing_agent/README.md) for all fields, tools, and data directories.

### XiaoQing Life

XiaoQing's own online life. The default state is disabled.

| Path | Default | Description |
|---|---|---|
| `plugins.xiaoqing_life.enabled` | `false` | Main switch. When disabled, the schedule does nothing, `life.turn_context` returns `None`, and chat behaves as without the plugin. |
| `plugins.xiaoqing_life.ai.routes.reasoning` | Necessary | Route for digesting, research, works, and the bedtime journal. Without it, browsing records "model not configured" and backs off. |
| `plugins.xiaoqing_life.ai.routes.vision` | None | Meme safety review. Without it, no memes are collected. |
| `plugins.xiaoqing_life.max_sessions_per_day` | `5` | Browsing sessions per day, by calendar day in the configured timezone |
| `plugins.xiaoqing_life.max_llm_calls_per_day` | `30` | All model calls combined. Each call is counted before it runs and is not refunded on failure. |
| `plugins.xiaoqing_life.browse_when_busy` | `false` | Whether a quick browse is allowed during a busy routine slot |
| `plugins.xiaoqing_life.blocked_keywords` | `[]` | Extra keywords whose content is discarded, on top of the built-in list |

Chat-side switches are `life.enable_online_life`, `life.online_life_timeout_seconds`, `life.enable_life_memes`, and `life.enable_life_share` in the `xiaoqing_chat` behavior configuration.
See [XiaoQing Life (Chinese)](../../plugins/xiaoqing_life/README.md) for all fields, commands, and data directories.

### QingSSH

Public budgets are under `plugins.qingssh`:

| Field group | Example fields | Function |
|---|---|---|
| Commands | `command_timeout_seconds=30` | Remote command budget. `0` assigns stopping to the administrator. |
| QQ actions | `qq_max_actions=6` | Actions per result |
| QQ text | `qq_max_text_chars=10000`, `qq_max_message_chars=1800` | Total-text and per-message budgets |
| Head/tail preview | `qq_head_chars=6000`, `qq_tail_chars=2000` | Kept long-output preview lengths |
| Delivery | `qq_send_interval_seconds=0.35`, `qq_send_timeout_seconds=5` | Action interval and per-send wait budget |
| Archives | `archive_max_bytes=67108864`, `archive_tail_bytes=1048576`, `archive_retention_files=20` | Archive-size, tail-size, and count budgets |

The remaining data lives in these places; back them all up together:

| Data | Location | Managed by |
|---|---|---|
| Server index (host, port, user, auth type, key path) | `data/qingssh/servers.json` | Administrator commands such as `/ssh add` and `/ssh remove` |
| Passwords | Plugin secret store; the index keeps only a `password_ref` | Written by `/ssh add`, reclaimed when a server is removed |
| Private keys | Local files referenced by `key_path` | Managed by the deployer |
| Host keys | `~/.ssh/known_hosts` of the current OS user | Written after the first confirmed connection |
| Hosts and jump settings from `~/.ssh/config` | `~/.ssh/config` of the current OS user | Read by `/ssh import` and `/sshconfig` |

If `servers.json` cannot be read, the plugin blocks further writes so an empty snapshot cannot overwrite the file; repair it and reload the plugin.
See [QingSSH configuration (Chinese)](../../plugins/qingssh/README.md) for the connection schema.

### Codex

Public job policies are under `plugins.codex`:

| Field group | Example fields | Function |
|---|---|---|
| Queue | `max_parallel_jobs=2`, `per_session_queue_limit=10` | Global concurrency and per-session waiting jobs |
| Process | `spawn_timeout_seconds=30`, `job_timeout_seconds=3600` | CLI startup and job deadlines |
| Output | `max_stdout_bytes`, `max_stderr_bytes`, `max_json_line_bytes`, `max_final_output_bytes` | Standard output, standard error, JSONL, and final-result budgets |
| QQ | `max_qq_text_chars=60000`, `max_qq_images=10` | Chat-text and image-count budgets |
| Artifact scan | `artifact_scan_max_entries=5000`, `artifact_scan_max_depth=8` | Job-artifact traversal budgets |
| Images | `max_image_artifacts`, `max_image_bytes`, `max_image_total_bytes`, `max_image_pixels`, `max_image_frames` | Image count, byte, and decode budgets |
| Sandbox | `sandbox="workspace-write"` | `read-only`, `workspace-write`, `danger-full-access` |
| Approval | `approval_policy="never"` | `untrusted`, `on-failure`, `on-request`, `never` |
| Repository | `skip_git_repo_check=true` | Codex CLI Git-repository check switch |

See [Codex configuration (Chinese)](../../plugins/codex/README.md) for working directories, allowed roots, retention, emergency disk thresholds, and arXiv summary sessions.

### Shell and Smalltalk

| Path | Example | Description |
|---|---|---|
| `plugins.shell.terminal.backend` | `"direct"` | `direct` starts external programs with `create_subprocess_exec`. `git-bash` executes command text through Git Bash. |
| `plugins.shell.terminal.executable` | Absolute Git Bash path | Interpreter path for `git-bash` |
| `plugins.smalltalk.voice_probability` | `0` | Probability of converting plain-text smalltalk to audio, `0..1` |

The Shell allowed-command list, working directory, and audit switch are under `secrets.plugins.shell`.
See [Shell configuration (Chinese)](../../plugins/shell/README.md) for both backends.
The [plugin manual](09-plugins.md) lists configuration entry points and data locations for all 32 plugins.

## ⚙️ Secrets

Core top-level secret fields:

| Field | Description |
|---|---|
| `inbound_token` | HTTP/WS inbound Bearer token |
| `onebot_token` | Active WebSocket and HTTP action Bearer token |
| `admin_user_ids` | Bot administrator QQ list |
| `ai.providers.<name>.api_key` | AI provider API key |
| `plugins.<name>` | Private plugin credentials |

Use a nonempty high-entropy `inbound_token` when inbound listeners are enabled.
Keep `onebot_token` consistent with the OneBot implementation. Empty strings select the agreed anonymous mode.
`admin_user_ids` uses positive integer QQ IDs.

Plugin fields in example secrets:

| Namespace | Fields | Purpose |
|---|---|---|
| `plugins.arxiv_filter` | `feishu_webhook` | Feishu notification webhook |
| `plugins.flickr` | `api_key` | Flickr App Garden public read-only API key |
| `plugins.wolframalpha` | `appid` | Wolfram\|Alpha App ID |
| `plugins.voice` | `subscription_key`, `region`, `voice_name`, `style`, `role`, `proxy` | Azure Speech credentials, voice settings, proxy |
| `plugins.chat` | `token`, `bot_id`, `proxy` | Coze API credentials and proxy |
| `plugins.twitter` | `user_id`, `headers.authorization`, `cookies`, `proxy`, `max_pages` | X/Twitter identity, session, page budget |
| `plugins.signin.yingshijufeng` | `app_id`, `kdt_id`, `access_token`, `sid` | Yingshijufeng Youzan sign-in credentials |
| `plugins.ads_paper` | `ads_token` | NASA ADS API token |

QingSSH, Shell, and Codex administrator commands maintain their secret namespaces.
Corresponding plugin READMEs define their schemas.
Use random high-entropy production tokens. Protect secrets through file permissions, backup policy, and host access control.
A reverse proxy can add source restrictions, rate limits, and outer authentication.

## ⚙️ Configuration live reload

Administrator command:

```text
/reload
```

Publication sequence:

1. `ConfigManager` repeatedly reads files to obtain a stable snapshot.
2. Core validates JSON, structure, ranges, addresses, and secret-source states.
3. ConfigManager creates a new revision.
4. The application serially publishes administrators, credentials, plugin views, AI routes, network endpoints, and schedule parameters.
5. Plugin configuration subscribers receive the same revision.

### Activation matrix

| Configuration category | Activation |
|---|---|
| Bot name, prefixes, default groups, sessions, concurrency, timezone | `/reload` or configuration watcher |
| OneBot addresses, switches, queues, tokens | Reconnect after confirmed revision publication |
| Inbound addresses, switches, workers, queues, tokens | Candidate listener generation change after confirmed revision publication |
| `plugin_execution` and plugin watcher policies | `/reload` or configuration watcher |
| AI providers, profiles, routes, API keys | Next AI call after confirmed revision publication |
| Public plugin configuration and private credentials | Plugin reads after confirmed revision publication |
| Valid external single-file `secrets.json` candidate | Administrator checks private field summary, then sends `/reload` |
| `data_root`, logging, `napcat_account`, `mkl_threading_layer` | Main-process or production-chain restart |
| Plugin source, manifest, `watch_files` | `/reload`, plugin watcher, or process restart in restart-only environments |

Invalid public configuration keeps the last known good snapshot.
Missing, damaged, unreadable, or unconfirmed secret sources immediately enter fail-closed state.
Core revokes network credentials and runtime administrator views.
Administrator private-chat `/set_secret` applies to existing secret paths. Core writes under a lock and publishes the revision.

The configuration watcher treats separately saved file generations as independent source events.
A valid external secrets file can become a pending candidate under three conditions.
The public configuration signature must match the confirmed version.
The current source pair must be trusted.

The external secrets file must be complete and valid.
Runtime continues with confirmed secrets. Revision and credential generation remain unchanged.
Core uses the current trusted OneBot channel to notify all current administrators privately.

Notifications show file-validity state and added, removed, or changed field paths. All field values remain hidden.
`/reload` rereads stable disk sources and publishes the candidate atomically.

New public configuration can publish first.
Secrets with an unconfirmed pairing to that source enter the revoked `INCONSISTENT` state.
Deployments with active WebSocket as their only control channel use this sequence: stop, atomically save both complete files, then start.
Startup confirms the new source pair.
Instances with independent protected inbound access can send `/reload` through that channel after files stabilize.

Each configuration file has an 8 MiB limit. JSON trees have maximum depth 64 and maximum node count 100000.
Core rejects non-finite numbers, repeated unstable reads, and sources that exceed budgets.
During a shutdown window, deployment scripts write temporary files, replace targets atomically, then start the service.

Address or token changes wake the active WebSocket task.
Candidate listeners commit the new inbound generation. Only the committed generation admits new events at any time.

## ✅ Production configuration checklist

- Bot name, command prefixes, and default groups match the intended QQ scenes.
- `inbound_token` contains a high-entropy random value.
- Inbound listeners bind to loopback or a controlled proxy network.
- The reverse proxy supplies TLS, source policy, and rate limits.
- `admin_user_ids` contains current administrators only.
- Secrets contain AI keys and plugin credentials.
- Administrators check field summaries and send `/reload` for valid single-file secret replacements.
- Public configuration changes and two-source replacements occur during shutdown windows.
- `data_root`, log directories, and backup directories have explicit owners.
- `plugin_execution` gives long-task plugins suitable budgets.
- Pendo Web Cookie Secure matches the HTTPS deployment.
- `bash scripts/run_full_uat.sh --plan-only` and full UAT complete before production startup.

## 🧭 Next steps

- First deployment: [getting started](01-getting-started.md)
- Plugin fields: [plugin manual](09-plugins.md)
- Network and message paths: [message flow](08-message-flow.md)
