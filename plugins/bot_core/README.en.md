# 🧭 Bot Core

[中文](README.md) | English

Bot Core supplies command help, runtime reload, plugin lists, group muting, sensitive-configuration management, and runtime metrics.

## 🔐 Requirements

- `/help` and `/plugins` are available in private and group chats.
- Bot administrator permission is necessary for `/reload` and `/metrics`.
- Bot administrator permission and a group chat are necessary for group mute commands.
- Bot administrator permission and a private chat are necessary for secret commands.
- Core issues the `secret_admin` capability according to the `/set_secret` declaration.

## ⌨️ Commands

| Command | Context and permission | Function |
| --- | --- | --- |
| `/help [query] [page N]` | Public | Browse or search the command catalog |
| `/help json [query] [page N]` | Public | Export a structured command catalog |
| `/reload` | Bot administrator | Reload configuration and plugins. Report the result and duration |
| `/plugins` | Public | List loaded plugins |
| `/闭嘴 [duration]` | Administrator, group chat | Pause ordinary replies in the current group |
| `/说话` | Administrator, group chat | Resume ordinary replies in the current group |
| `/set_secret <path> <value>` | Administrator, private chat | Update an existing secret path |
| `/get_secret <path>` | Administrator, private chat | Show a redacted value or object keys |
| `/metrics` | Bot administrator | Show uptime, calls, success rate, and slow plugins |

| Function | Primary command | Manifest aliases |
| --- | --- | --- |
| Help | `/help` | `/h` `/帮助` `/commands` `/命令目录` `/catalog` |
| Reload | `/reload` | `/重载` |
| Plugin list | `/plugins` | `/插件` |
| Group mute | `/闭嘴` | `/shutup` `/mute` `/安静` |
| Group resume | `/说话` | `/speak` `/unmute` |
| Write secret | `/set_secret` | `/setsecret` `/设置密钥` |
| Read secret | `/get_secret` | `/getsecret` `/查看密钥` |
| Metrics | `/metrics` | `/stats` `/性能` `/指标` |

`/help bot_core` shows complete subcommands, permissions, contexts, and invalid examples.

## ⌨️ Hierarchical help

Help supplies level-by-level navigation for mobile reading:

```text
/help                         # Functional areas and plugins
/help pendo                   # Plugin entry point
/help pendo todo              # Direct subcommands
/help pendo todo add          # Leaf details
/help pendo.pendo.todo.add    # Stable command code
/help search 提醒             # Keyword search
/help json pendo              # JSON catalog
```

Overview, plugin, branch, and search pages support `page N`.
Leaf pages show usage, aliases, permissions, contexts, valid examples, and invalid examples together.
Long usage lines wrap at argument boundaries.

## 📌 Reload

`/reload` first publishes a configuration revision. It then asks Core to create a background plugin reload task.
The first reply confirms configuration publication and the start of plugin reload.
The background task waits for the current `bot_core` call to release its execution gate.

It then scans, validates, and publishes the plugin generation.
At completion, it sends one success or failure result and duration to the original administrator chat.
Repeated commands that reuse the same background task register only one completion notification.

An external tool can save a complete valid `secrets.json` while public configuration matches the confirmed version.
The watcher then keeps the trusted runtime generation and stages the candidate.
Core privately sends current administrators the paths of added, deleted, and changed fields. The message contains paths only.

`/reload` reads the disk again and confirms the candidate.
A service-stop window is necessary for public-configuration changes and complete replacement of both sources.
An instance with a separate protected Inbound channel can use that channel for `/reload` after files stabilize.

## 💬 Group mute

An empty duration selects 10 minutes. Supported forms include minutes, hours, and Chinese units:

```text
/闭嘴
/闭嘴 30
/闭嘴 30m
/闭嘴 30min
/闭嘴 1.5h
/闭嘴 2小时
/闭嘴 30分钟
```

The duration must be greater than zero and at most 24 hours. `/说话` clears the current group's mute.
Mute applies to ordinary chat and automatic URL parsing.
Business commands and active Sessions follow their respective contracts.

## ⚙️ Secret management

### Paths

Path segments contain letters, digits, underscores, or hyphens. A `.` separates segments:

```text
plugins.signin.yingshijufeng.sid
plugins.demo.api-key
admin_user_ids
```

`set_secret` updates existing paths in the secrets tree.
`SecretAdminService` and `ConfigManager` perform the write and publish a new revision after a successful commit.
The command updates existing API keys, tokens, and plugin credentials during operation.
New paths can be added through a complete valid `secrets.json`.
The administrator examines the field summary and confirms it with `/reload`.

### Values

Standard JSON values keep their types. Other input is stored as a string:

```text
/set_secret plugins.demo.enabled true
/set_secret plugins.demo.retries 3
/set_secret plugins.demo.tags ["a", "b"]
/set_secret plugins.demo.options {"mode": "safe"}
/set_secret plugins.demo.token plain-text-token
```

### Redacted display

| Value type | Display |
| --- | --- |
| Short string | `****` |
| Long string | First and last two characters, with `****` between them |
| Number or Boolean | `****` |
| List | Item count |
| Object | Key count or at most 20 direct key names |
| Other type | `[hidden]` |

Command arguments use log redaction.
A controlled administrator private chat is necessary for production operations. Credentials must be rotated when chat-history exposure creates a risk.

## 🛡️ Runtime metrics

`/metrics` includes uptime, total calls, success rate, average duration, slow-call count, error count, and up to five slowest plugins.
Missing or malformed observations display `n/a`.
Plugin names pass newline and whitespace cleanup. The reply also shows metric-service status.

## 💾 Data and lifecycle

Bot Core uses Core configuration, metrics, and mute services.
The plugin owns lightweight command logic. The corresponding Core services manage runtime data.

## 🩺 Troubleshooting

1. Use `/help bot_core` to examine the current command catalog.
2. Use `/plugins` to examine loading state.
3. Use `/metrics` to examine plugin errors and latency.
4. Use log request IDs to trace reload, permissions, and secret revisions.

## ✅ Development checks

```bash
python -m pytest -q \
  tests/plugins/bot_core/test_bot_core.py \
  tests/plugins/contracts/test_internal_log_redaction.py \
  tests/plugins/contracts/test_public_error_redaction.py
python -m ruff check plugins/bot_core tests/plugins/bot_core/test_bot_core.py
python -m mypy plugins/bot_core
```
