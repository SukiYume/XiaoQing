# 💬 Chat

[中文](README.md) | English

`chat` uses Coze API v3 for single-turn AI conversations. All users can use the command. Daily user and global quotas control use.
The plugin also supplies the `chat.reply` service to the authorized `smalltalk` plugin.

## 🔐 Requirements

- A published Coze agent is available.
- The deployment has a Coze personal access token and agent ID.
- Core has loaded `plugins/chat/plugin.json`.
- The service caller list includes `smalltalk` when that plugin uses the service.

## ⌨️ Commands

| Function | Primary command | Manifest aliases |
| --- | --- | --- |
| Coze single-turn conversation | `/chat` | `/gpt` `/ai` |

```text
/chat <question>
/chat help
```

The question must contain 1–2000 characters after removal of whitespace at both ends. `help` and `帮助` show local help.

## ⚙️ Configuration and credentials

The Coze credentials and optional proxy are in `config/secrets.json`:

```json
{
  "plugins": {
    "chat": {
      "token": "pat_your-coze-api-token",
      "bot_id": "your-coze-bot-id",
      "proxy": ""
    }
  }
}
```

| Field | Necessary | Description |
| --- | --- | --- |
| `token` | Yes | Coze personal access token |
| `bot_id` | Yes | Published agent ID |
| `proxy` | No | `aiohttp` proxy address. An empty string selects a direct connection |

Daily quotas are in `config/config.json`:

```json
{
  "plugins": {
    "chat": {
      "daily_user_limit": 20,
      "daily_global_limit": 100
    }
  }
}
```

Each quota accepts integers from `1` to `1000000`. The Core global `timezone` setting determines the service date.

## 🔄 Requests and quotas

A request creates a conversation, polls its status, and reads its messages. It returns the first nonempty `answer` text.
These operations share a 30-second limit. The plugin permits two concurrent Coze calls. Timeout cleanup has a separate 3-second limit to cancel the remote conversation.

The plugin reserves quota atomically before the remote call. It commits the reservation after a successful answer.
Network errors, protocol errors, timeouts, cancellation, and empty answers release the reservation. This file contains quota state:

```text
data/chat/chat_quota.json
```

If a thread has started a reservation write, cancellation waits for the result. It then releases the reservation.
This compensation also applies before the caller receives the reservation token.

`chat.reply` uses the same configuration, quotas, and remote request procedure. It accepts text and a signed call context from `smalltalk`.

## 🔐 Data and privacy

The plugin combines `bot_id` with the caller ID and calculates a SHA-256 digest.
It sends the first 32 hexadecimal characters to Coze as the user identifier.
Logs contain query length, status, message count, and safe error codes. Log filtering excludes questions, answers, tokens, proxy credentials, and caller identifiers.

Coze requests use the Core HTTP session. Checks control response size, JSON depth, MIME type, redirects, and decompression size.

## 🩺 Troubleshooting

| Symptom | Check |
| --- | --- |
| Missing configuration | Examine the location, spelling, and values of `token` and `bot_id`. |
| Quota reached | Examine both daily quotas and the Core `timezone`. |
| Request timeout | Examine Coze, the proxy connection, and safe error codes in the logs. |
| Rejected `smalltalk` call | Examine the service caller list in `plugin.json` and the call signature. |

## ✅ Development checks

Run these commands from the repository root:

```bash
python -m ruff check plugins/chat tests/plugins/chat/test_chat.py
python -m mypy plugins/chat
python -m pytest -q tests/plugins/chat/test_chat.py \
  tests/plugins/contracts/test_public_error_redaction.py \
  tests/plugins/contracts/test_configured_http_clients.py \
  tests/plugins/contracts/test_plugin_resource_lifecycle.py
```
