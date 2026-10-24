# ⛏️ Minecraft

[中文](README.md) | English

`minecraft` lets Bot administrators connect to Minecraft Java Edition RCON from private or group QQ chats.
It forwards locally readable server-log events to that chat.
After connection, all group members can chat with game players. Deployment tools manage the Java server process.

## 🔐 Permissions and commands

All three Manifest commands support private and group chats.
Bot administrator permission is necessary for connection, disconnection, and `/mc run`.
Their `plugin.json` subcommand declarations define this requirement, and Core examines it.
All users can use `/mc say`, `/mc tell`, `/mc list`, `/mc status`, and `/mc help` with the current chat's connection.

| Function | Primary command | Manifest aliases |
| --- | --- | --- |
| Minecraft management | `/mc` | `/minecraft` |
| Direct connection | `/mcconnect` | `/mc连接` |
| Direct disconnection | `/mcdisconnect` | `/mc断开` |

| Usage | Description |
| --- | --- |
| `/mc help` | Show local help |
| `/mc connect <profile>` | Read a local profile and connect |
| `/mc status` | Show connection and log-monitor status for this chat |
| `/mc list` | List online players. All users |
| `/mc say <message>` | Speak to all online players under the sender's QQ name. All users |
| `/mc tell <player> <message>` | Speak privately to one player. All users |
| `/mc run <server-command>` | Execute one literal RCON command. Bot administrators |
| `/mc disconnect` | Close this chat's connection |
| `/mcconnect <profile>` | Same as `/mc connect <profile>` |
| `/mcdisconnect` | Same as `/mc disconnect` |

`/mc say` and `/mc tell` use `tellraw` text components.
The game shows `[QQ] <QQ name> message`. Messages become one line with a 256-character limit.
Selectors such as `@a` and `@e`, and commands within messages, remain literal text.
Player names for `/mc tell` contain 1–16 letters, digits, or underscores.
Bot administrators send other content literally through RCON.

## ⚙️ Configuration

Connection profiles are in `plugins/minecraft/config.json`:

```json
{
  "default": {
    "host": "127.0.0.1",
    "port": 25575,
    "log_file": "C:/minecraft/logs/latest.log"
  },
  "staging": {
    "host": "mc.internal.example",
    "port": 25575,
    "log_file": ""
  }
}
```

Matching profile names in `config/secrets.json` contain RCON passwords:

```json
{
  "plugins": {
    "minecraft": {
      "default": "replace-with-a-strong-password",
      "staging": "replace-with-another-strong-password"
    }
  }
}
```

| Field | Rule |
| --- | --- |
| Profile name | 1–64 letters, digits, underscores, dots, or hyphens |
| `host` | Structurally valid hostname or IP. Maximum 253 characters |
| `port` | Integer from 1 to 65535. Default: 25575 |
| RCON password | Matching entry in `config/secrets.json`. Maximum 4096 UTF-8 bytes |
| `log_file` | Empty string disables forwarding. Relative paths use `plugins/minecraft/` |

The profile file limit is 64 KiB.
A log-path read error leaves the RCON connection available for server commands.

## 🔐 RCON security

The Minecraft server must enable RCON with the profile's port and password.
Source RCON transmits passwords and commands as plain text. Recommended network controls are:

- A server listener on the loopback address.
- SSH local port forwarding or a protected tunnel for cross-host connections.
- A firewall rule that permits only the Bot host.
- A separate strong RCON password.

See the [Valve Source RCON Protocol](https://developer.valvesoftware.com/wiki/Source_RCON_Protocol) for the protocol format.

## 🔐 Connection and response limits

- Each private or group chat has a separate connection.
- Reconnection publishes the new connection atomically and closes the old connection.
- Connection, authentication, commands, and closure execute sequentially within each connection.
- Commands have a limit of 4096 UTF-8 bytes.
- Protocol checks cover little-endian lengths, request IDs, packet types, double NUL, UTF-8, and accumulated responses.
- Each response has a memory limit of 1 MiB.
- QQ text has limits of 4000 characters and 12 KiB. Cleanup removes ANSI, C0, and C1 control sequences.
- Packet boundaries use both UTF-8 bytes and Java UTF-16 units.
- A continuation timeout, partial packet, EOF, or protocol failure closes the connection.
- Before the next command, reconnection is necessary to obtain a clean response stream.
- Logs contain types, lengths, byte counts, and one-way fingerprints for commands, responses, and destinations.

## 💾 Log forwarding

With `log_file` configured, the Manifest schedules examination of Minecraft `INFO` logs every 5 seconds.
Recognized events are player chat, joins, departures, deaths, and advancements.
Messages go to the private or group chat that owns the connection.
For group forwarding, membership in `default_group_ids`, the `check_log` schedule's target groups, is necessary.

Other groups receive a connection notice that only commands are available.
Removal from the target list pauses polling for an existing group connection.
The initial cursor starts at the file end. Subsequent progress is stored here:

```text
data/minecraft/log_cursors/
```

The plugin commits the cursor after explicit OneBot send confirmation.
Send errors, timeouts, and pending batches keep the cursor for the next poll.
The monitor detects log rotation, truncation, and a final partial line.

## 📌 Forwarding limits

| Item | Limit |
| --- | ---: |
| File read per pass | Latest 1 MiB |
| Kept matching events per pass | 1000 |
| Events per connection per round | 12 |
| Actions globally per round | 5 |
| One action | 1800 characters, 6000 UTF-8 bytes |
| Token bucket | Capacity 24. Refill 0.5 per second |

The scheduler rotates the initial connection to distribute delivery opportunities fairly.
Events beyond the display limit appear as a count summary.

## ⏰ Lifecycle

Each connection has a separate RCON lock.
The 5-second schedule uses Core `targeted` mode and a global lock for sequential polling.
Group targets follow the schedule target list. Connection owners determine private targets.
At unload, reload, or Bot shutdown, `shutdown()` removes and closes all RCON connections and clears process-local rate-limit state.
Log cursors remain in the data directory for restart recovery.

## 🩺 Troubleshooting

| Symptom | Check |
| --- | --- |
| RCON authentication failure | Examine server settings, profile name, and secret password. |
| RCON connection failure | Examine the listener address, port, firewall, and tunnel. |
| `/mc say` error | Examine the connection with `/mc list`, then examine server command permissions. |
| QQ receives player login messages | This confirms the log-forwarding and cursor-commit path. |
| Log reception problem | Examine `log_file`, Bot account read permissions, and `/mc status`. |
| Truncated response | Reduce the server command output or read the complete result in the server console. |

## ✅ Development checks

Run these commands from the repository root:

```bash
python -m pytest -q tests/plugins/minecraft/test_minecraft.py \
  tests/plugins/minecraft/test_minecraft_flood.py \
  tests/plugins/minecraft/test_minecraft_rcon_results.py
python -m ruff check plugins/minecraft
python -m mypy plugins/minecraft
```
