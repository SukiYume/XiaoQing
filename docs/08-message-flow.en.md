# 🔄 08 - Message flow

[中文](08-message-flow.md) | English

This chapter follows one OneBot event from network input to message delivery.
Use it to diagnose connection, routing, session, plugin-call, and reply-delivery problems.

## 🔄 Flow overview

```text
OneBot Event
  → network input and authentication
  → event-model validation
  → MessageContext
  → Dispatcher A–G stages
  → plugin execution gate
  → plugin-result normalization
  → OneBot Action
  → HTTP / WebSocket delivery
  → send receipts and metrics
```

## 🌐 1. Network input

### Passive inbound

- `POST /event` receives HTTP events.
- `WebSocket /ws` receives WebSocket events and sends actions.
- Both entries share `inbound_token`, admission queues, and session ordering.

Before admission to the processing queue, requests pass these checks:

1. Listener generation state
2. Bearer token
3. Request body and byte budget
4. JSON parsing
5. OneBot event-model validation
6. Current token-revision recheck

### Active WebSocket

`OneBotWsClient` connects to `onebot_ws_uri`, reads event JSON, and passes it to the same application dispatch entry.
The connection task owns backoff, jitter, configuration changes, and shutdown cleanup.

## 🔄 2. Event normalization

`OneBotEvent` validates common fields:

```json
{
  "post_type": "message",
  "message_type": "group",
  "user_id": 123456789,
  "group_id": 987654321,
  "message_id": 456,
  "message": [
    {"type": "text", "data": {"text": "/help"}}
  ],
  "sender": {
    "user_id": 123456789,
    "role": "member"
  }
}
```

String messages and segment lists become normalized message segments.
`core.message` extracts text, replies, mentions, images, face, mface, and other media signals.

## 🧩 3. MessageContext

Dispatcher builds `MessageContext` from the event. Key fields follow:

| Field | Description |
|---|---|
| `event` | Normalized OneBot event |
| `text` | Text extracted from segments |
| `clean_text` | Text after Bot-name and command-prefix processing |
| `user_id` | Current user |
| `group_id` | Current group, empty in private chat |
| `message_id` | OneBot message ID |
| `has_command_prefix` | Original message contains a command prefix |
| `has_prefix` | Original message contains a command prefix or Bot name |
| `is_private` | Private-chat flag |
| `is_at_me` | Bot-mention flag |
| `is_reply_to_bot` | Reply-to-Bot flag |
| `principal` | User, group role, administrator capabilities |
| `request_id` | Request tracking ID |

Bot names and command prefixes follow configuration order. Example:

```text
Input: 小青 /help pendo
After Bot-name processing: /help pendo
After command-prefix processing: help pendo
Router input: help pendo
```

## 🔄 4. Processing gates

Stage A uses these signals to decide whether a message enters business processing:

- Private chat
- Command prefix
- Bot name
- Bot mention
- Active session
- Ordinary group messages permitted by configuration
- Smalltalk Provider observation requirements

Group mute state affects ordinary-chat and URL paths at this stage.
Commands and sessions continue under their own permission and scene contracts.

## 🔄 5. Dispatcher A–G stages

### A. Processing gates

This stage establishes message context and checks group rules, identity, and mute state.

### B. URL parsing

When `clean_text` is one HTTP/HTTPS URL, Dispatcher calls the URL provider.
The URL passes the safe-network boundary before content extraction.

### C. Bot-name response

When the message contains only the Bot name, Dispatcher creates a name response or calls the Smalltalk Provider name scene.

### D. Active session

When the current user key has a session, Dispatcher calls its owner plugin:

```python
await plugin.handle_session(text, event, context, session)
```

The same session key executes serially.
Plugins commit working copies with `update_session()` or end the flow with `end_session()`.
Before the call, `command_access` rechecks current access to at least one top-level command from the session plugin.
Revoked permission or a prohibited scene closes the session and returns an explanation.

### E. Command

Router resolves top-level triggers and recursive subcommands from the command catalog:

```text
/pendo todo add 写周报
  → plugin: pendo
  → root: pendo
  → child: todo
  → leaf: add
  → arguments: 写周报
```

These steps precede the call:

1. Match commands and aliases.
2. Inherit subcommand permissions and scenes.
3. Use `command_access` to check `bot_admin` / `group_admin` permission, then private/group scene.
4. Authorize the plugin generation.
5. Admit the call through the plugin execution gate.

`command_access` is the project-wide permission decision function.
`context.command_invocation` gives the parsed result to the plugin.

### F. Command fallback

When prefixed text has no catalog match, Dispatcher returns a help message and query entry point.

### G. Smalltalk Provider

Ordinary-chat candidates enter the configured `smalltalk_provider`.
`xiaoqing_chat` can run attention, rate control, behavior planning, AI generation, and reply checking internally.
On explicit invocation, `xiaoqing_chat` asks `xiaoqing_agent` through `agent.handle_turn` whether to execute a task for this turn.
The initiator's permissions determine available tools.

Chat generation runs concurrently. A chat decision uses that reply. Agent takeover discards it.
When the agent is disabled, chooses chat, or fails, the original flow continues.

The observation entry receives ordinary-chat candidates.
Earlier stages handle commands, URLs, sessions, and command-prefixed text.

Agent task commands use `Dispatcher.run_command_text`.
They execute only stage E as the initiator, with the same permission and scene checks as direct messages.
They use a one-time session namespace.
New sessions pass to plugin `close_session` for cleanup before disposal.

Each call has an independent operation identifier.
The request ID has format `original-request-ID.command.<operation-id>`.
Command `DeliverySegments` and receipts return unchanged to the agent.

## 🔄 6. Router

Router builds its command catalog from validated manifests. Each node contains:

- Stable code
- Canonical name and aliases
- Usage and help
- Permission
- Scenes
- Valid and invalid examples
- Direct children

At startup and reload, Router detects top-level trigger conflicts at equal priority.
The published catalog remains read-only.
`/help`, JSON export, permission checks, and plugin calls share this data.

## 💬 7. Session path

Session keys:

```text
Private: (user_id, None)
Group: (user_id, group_id)
```

Session flow:

```text
create_session()
  → SessionManager stores initial values
  → later events enter stage D
  → get_session() returns an isolated snapshot
  → update_session(callback) commits atomically
  → end_session() or idle timeout ends the session
```

One plugin owns each session.
The plugin name selects `handle_session()`. The user key supplies serialization and isolation.

## 🧩 8. Plugin execution

Commands, sessions, smalltalk, URLs, schedules, and declared services all enter plugin runtime authorization.
Execution chain:

```text
Plugin generation authorization
  → admission queue
  → concurrency gate
  → timeout / circuit policy
  → handler
  → result normalization
  → generation release
```

`parallel` plugins use configured concurrency. `sequential` plugins permit one concurrent entry.
Synchronous functions enter separate synchronous budgets through `run_sync()`.

## 🔄 9. Return-value normalization

Plugin entries can return:

- String
- OneBot segment list
- `DeliverySegments` with a receipt
- OneBot action list
- Empty result

Core converts strings to text segments and segments to actions for the current user or group.
It checks file, image, audio, and long-text boundaries.

## 🔄 10. Outbound delivery

### HTTP action

`OneBotHttpSender` sends actions to `onebot_http_base` with optional `onebot_token`.
Standard HTTP reports also use this actual delivery channel. The event response is `{}`.
Development clients explicitly set `X-XiaoQing-Response-Mode: actions` to receive action lists and execute delivery themselves.
HTTP-report Bearer credentials and OneBot action API credentials have separate configuration.

### WebSocket action

Active WebSocket connections and inbound WebSocket sessions can carry actions.
Worker count, queues, and per-connection budgets constrain broadcasts.

### Outbound observation

Nonsensitive plugin actions notify the `core.observe_outgoing_action` provider, currently `xiaoqing_chat`.
Chat context thus receives other plugins' Bot notifications.
XiaoQing Chat records its own replies at receipt commit, so this path skips them.
Actions with `SENSITIVE_ACTION_KEY`, such as Pendo login codes, bypass observation.
The marker is removed before transport.

### Receipts

Delivery returns transport results to receipt objects.
Plugins with commit-after-ack requirements commit reminder, task, or notification state after successful delivery.
For split messages, transport records each confirmed physical message ID before its result.
After all acknowledgements, the commit callback reads every ID from `message_ids`.
XiaoQing Chat maps persona messages to actual messages with these IDs.
It can recognize a group member's reply to any split part.

The event-scoped action sink stages actions and owns their subsequent settlement.
When `send_with_receipt` observes staged state, it leaves receipts pending.
Final success, definite failure, and unknown results enter their respective callbacks.
Active WebSocket connection notifications and the read loop run concurrently.
The read loop consumes echo receipts. Connection closure cleans up notification tasks.

## 🔄 11. Concurrency and ordering

### Inbound admission

HTTP and WebSocket share a scheduler keyed by session:

- Each private user executes serially in admission order.
- Each group/user pair executes serially in admission order.
- Different session keys can execute concurrently.
- `inbound_ws_max_workers`, range `1..128`, controls active workers.
- `ws_queue_size`, range `1..10000`, controls the waiting backlog.
- Total admission capacity equals workers plus backlog. HTTP and WebSocket share one bounded scheduler.

### Dispatcher

`max_concurrency` limits globally active message handlers.

### Plugins

`plugin_execution` limits each plugin's entry queues, concurrency, timeouts, circuit breaking, and synchronous tasks.

### Configuration changes

Listeners, tokens, administrator state, and plugin contexts publish by revision.
Queued items recheck current authorization before execution.

## 💬 12. Mute semantics

`/闭嘴` pauses ordinary replies in the current group for a specified period.
`/说话` clears that state.
Mute affects ordinary chat and automatic URL parsing.
Administrator commands, permitted business commands, and active sessions continue under their own contracts.
`context.get_mute_remaining(group_id)` returns minutes.

## 🩺 13. Observation and troubleshooting

Each event turn has a request ID. Check logs in this order:

1. Inbound or active WebSocket receipt
2. Token and event-model validation
3. MessageContext gate signals
4. Current Dispatcher stage
5. Router command code and permission result
6. Plugin generation, queue, handler
7. OneBot action and send receipt

`/metrics` supplies message counts, plugin calls, errors, latency, and queue metrics.

## 🛠️ Key code locations

| Stage | File |
|---|---|
| Inbound HTTP/WS | `core/server.py` |
| Active WebSocket and HTTP actions | `core/onebot.py` |
| Event and message-segment models | `core/models.py`, `core/message.py` |
| MessageContext and A–G stages | `core/dispatcher.py` |
| Command catalog and parsing | `core/router.py` |
| Permission and scene decisions | `command_access` in `core/router.py` |
| Agent command executor | `run_command_text` in `core/dispatcher.py` |
| Session | `core/session.py` |
| Plugin generations and execution | `core/plugin_generation.py`, `core/plugin_runtime.py`, `core/plugin_execution.py` |
| Outbound receipts | `core/delivery.py`, `core/app_delivery.py` |
| Configuration revision | `core/config.py`, `core/app_config_apply.py` |

## 🧭 Next steps

- Component relationships: [architecture](02-architecture.en.md)
- Configuration and network boundaries: [configuration](06-configuration.en.md)
- Plugin entries: [plugin development](03-plugin-development.en.md)
