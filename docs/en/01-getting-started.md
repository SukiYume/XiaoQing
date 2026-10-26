# 🚀 01 - Getting started

[中文](../01-getting-started.md) | English

This chapter covers dependency installation, configuration, OneBot connection, startup, and initial checks from a fresh source directory.
Command examples apply to Git Bash, macOS, and Linux.

## ⚙️ Prepare the environment

| Item | Requirement |
|---|---|
| Python | `3.11+`, available through the `python` command |
| OneBot | A OneBot v11 implementation. NapCatQQ is recommended |
| Dependencies | The current environment can install `requirements.txt` with pip. |
| Network | Access to QQ and third-party services used by enabled plugins |

Users manage Python, virtual environments, and dependency installation. The common XiaoQing startup command is `python main.py`.

## ⚙️ 1. Get the source and install dependencies

```bash
git clone https://github.com/SukiYume/XiaoQing.git
cd XiaoQing
python -m pip install -r requirements.txt
```

The root dependency list covers Core, default plugins, and Pendo Web. Additional resources are necessary for these functions:

- An available Jupyter kernel is necessary for the Jupyter plugin.
- Local arXiv Filter inference uses model assets in `plugins/arxiv_filter/best_model/`.
- Service credentials are necessary for Flickr, Wolfram|Alpha, voice, and other external API plugins.

## ⚙️ 2. Create configuration files

```bash
cp config/config.json.example config/config.json
cp config/secrets.json.example config/secrets.json
```

### Public configuration

Check these fields in `config/config.json` before the first startup:

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

### Secret configuration

Set at least the inbound authentication token and administrator QQ IDs in `config/secrets.json`:

```json
{
  "inbound_token": "replace-with-a-random-secret",
  "admin_user_ids": [123456789]
}
```

Keep `config/secrets.json` on the deployment host. Set file read permissions for the Bot process identity.
The project Git ignore rules cover the active secrets file.

Write the complete configuration before the first startup.
During operation, an administrator can send `/set_secret <existing-path> <value>` in private chat to submit one secret change.
To add paths, write a complete, valid `secrets.json` file.
The watcher stages this candidate when `config.json` remains at the currently confirmed version.

It privately lists added, removed, and changed fields to current administrators. It hides all credential values.
Check the notification. Send `/reload` to confirm and apply the candidate.

To change `config.json` or replace both sources, stop the service first. Save complete files, then start a new process.

### AI plugin configuration

`xiaoqing_chat`, Pendo AI parsing, and other model calls use the common AI registry:

```json
{
  "ai": {
    "providers": {
      "deepseek": {
        "api_base": "https://api.deepseek.com",
        "endpoint_path": "/chat/completions"
      }
    },
    "models": {
      "deepseek-flash": {
        "provider": "deepseek",
        "model": "deepseek-flash",
        "modalities": ["text"]
      }
    }
  },
  "plugins": {
    "xiaoqing_chat": {
      "ai": {
        "routes": {
          "chat": {
            "models": ["deepseek-flash"]
          }
        }
      }
    }
  }
}
```

Put the corresponding API key in `config/secrets.json`:

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

See [configuration](06-configuration.md) for all fields, model fallback, plugin configuration, and security boundaries.

## ⚙️ 3. Configure OneBot

### Passive inbound

In passive mode, OneBot sends events to XiaoQing.
Set the reporting URL to `http://127.0.0.1:12000/event`. Configure these request headers:

```http
Authorization: Bearer replace-with-a-random-secret
Content-Type: application/json
```

Use this XiaoQing configuration:

```json
{
  "enable_ws_client": false,
  "enable_inbound_server": true,
  "onebot_http_base": "http://127.0.0.1:11001",
  "inbound_http_base": "http://127.0.0.1:12000"
}
```

HTTP `/event` and WebSocket `/ws` share `inbound_token`, session ordering, and a bounded admission queue.
Set a nonempty `inbound_token` when you enable an inbound listener.

Enable the OneBot HTTP API. Configure the reply channel with `onebot_http_base` and `secrets.onebot_token`.
Standard event reports receive a `{}` acknowledgement. The action API sends the messages.

Configuration with only `secret`/`X-Signature` receives HTTP 401. The Bearer request header is necessary for HTTP event reports.
The development matrix explicitly requests an action-list response with `X-XiaoQing-Response-Mode: actions`.

### Active WebSocket

In active mode, XiaoQing connects to OneBot:

```json
{
  "enable_ws_client": true,
  "enable_inbound_server": false,
  "onebot_ws_uri": "ws://127.0.0.1:11000/ws",
  "onebot_http_base": "http://127.0.0.1:11001"
}
```

If OneBot enables Bearer authentication, set top-level `onebot_token` in `config/secrets.json`.
An empty string in a valid secrets snapshot selects the anonymous connection mode agreed by both services.

### Network boundary

Inbound binds to loopback by default. For public deployment, use Nginx or Caddy to terminate HTTPS/WSS and forward traffic to loopback.
On a controlled network between containers, combine `inbound_trusted_tls_proxy: true` with firewall rules.

## 🚀 4. Start the service

Start the OneBot implementation. Then start XiaoQing from the project root:

```bash
python main.py
```

Successful startup logs contain these items:

- Published configuration revision
- Loaded plugin list
- Inbound Server or active WebSocket status
- Scheduler startup status
- Status of embedded plugin services, such as Pendo Web

Windows production deployments can use this protected startup chain:

```text
scripts/run-bot.vbs
  → scripts/run-bot-monitor.ps1
  → scripts/run_process_with_rotating_logs.py
  → python main.py
```

The chain calls Python from the current `PATH`.
It reads the NapCat account and optional MKL threading configuration from `config/config.json`.

Use this restart procedure for Windows production:

1. Double-click `scripts/stop-bot.vbs`.
2. Wait for the completion message that confirms the monitor, Bot, and NapCat have stopped.
3. Double-click `scripts/run-bot.vbs` to start new processes.

The stop entry point identifies this deployment with a repository-level mutex and absolute command paths.
It stops this repository's PowerShell monitor, Python log pump, `main.py`, and specified NapCat process tree.
Repeated double-clicks return the same completion result.
An elevated SSH session, scheduled task, or administrator terminal can start the monitor.
After repeated identity-read failures, the stop entry point shows one UAC prompt.
The elevated process repeats all identity checks before it stops the process tree.

## ✅ 5. Initial checks

Send these commands in sequence in administrator private chat:

```text
/help
/plugins
/metrics
/help bot_core
/help pendo
/help xiaoqing_chat
```

Then check the core plugins:

```text
/xc 你好
/pendo
/pendo todo list
/arxiv
```

Command help comes from the manifest. It shows entry points, parameters, permissions, scenes, and examples for the loaded version.

## ✅ Checks before deployment

### Automated checks

```bash
python -m compileall -q core plugins tests
python -m pytest -n 2
python -m ruff check .
python scripts/format_code.py --check
python -m mypy core plugins
git diff --check
```

### Full UAT

```bash
bash scripts/run_full_uat.sh --plan-only
bash scripts/run_full_uat.sh
```

UAT uses the current environment's interpreter to start a real `python main.py` process.
It redirects plugin data to the current report directory and runs these phases in sequence.
Finally, it shuts down gracefully and restores `config/config.json` byte for byte.

| Phase | Content |
|---|---|
| `ws-matrix` / `http-matrix` | Valid examples, invalid examples, permissions, and scenes for all manifest commands through WebSocket and HTTP inbound |
| `core-pressure` | Core concurrency, overload, and recovery load tests |
| `compileall` / `ruff` / `mypy` | The same static checks as CI |
| `pytest` | Full test suite |
| `diff` | `git diff --check` |
| `chat-quality` | Real-model chat-quality probes. Set `--include-chat-quality` explicitly. |

Reports are in `test_reports/runs/project/full-uat-*/reports/FULL_UAT_REPORT.md`. Common options follow:

| Option | Function |
|---|---|
| `--plan-only` | Print the plan. Keep the service stopped. |
| `--phases ws-matrix,pytest` | Run the specified phases. |
| `--matrix-plugins <plugin>` / `--matrix-codes <command-code-prefix>` / `--matrix-kinds <kind>` | Retest selected parts of the command matrix. |
| `--include-external --scenario-fixtures <file>` | Include business scenarios that depend on external services. |
| `--include-chat-quality` | Include the chat-quality phase, which consumes model quota. |
| `--output <directory>` | Set the report directory. |
| `--recover` | Restore configuration after forced UAT termination. First confirm that all services have stopped. |

## 🩺 Troubleshooting

### Service message response

1. Confirm that OneBot and XiaoQing processes are running.
2. Check the inbound URL or active WebSocket URI.
3. Check the report's Authorization Bearer header against `inbound_token`.
4. Check `onebot_token` for the reply channel.
5. Find connection status, error codes, and request IDs in the logs.
6. When a secret confirmation notification arrives, check its field paths and send `/reload`.
7. If logs contain both `secrets source is inconsistent` and `WebSocket client stopped`, stop the service.
8. Confirm that both configuration files are complete, then restart the service.

### Group command response

1. Confirm that the message has a command prefix or satisfies the Bot-name gate.
2. Confirm that the command permits the group scene.
3. Confirm that the current QQ identity satisfies administrator requirements.
4. Use `/help <command>` to check scenes and permissions.

### Few ordinary chat replies

1. Confirm that `plugins.smalltalk_provider` names the intended plugin.
2. Check the current group mute state.
3. Check attention, rate-control, and behavior-planning logs for `xiaoqing_chat`.

### Port binding errors

1. Check `onebot_http_base`, `onebot_ws_uri`, `inbound_http_base`, and the Pendo Web port.
2. Confirm that one service owns each listening address.
3. Restart the service after you change configuration.

### Pendo Web repeatedly shows the login page

1. Run `/pendo web status`.
2. Run `/pendo web token` to get a login code with a 7-day redemption period.
3. Redeem the code on the login page.
4. Permit the browser to save cookies.
5. Check session-validation results in the Pendo Web logs.

## 🧭 Next steps

- Production parameters and security: [configuration](06-configuration.md)
- Plugin functions: [plugin manual](09-plugins.md)
- Message troubleshooting: [message flow](08-message-flow.md)
