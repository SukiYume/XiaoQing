# 🔌 05 - API reference

[中文](../05-api-reference.md) | English

This chapter lists public types, functions, and entry signatures used in plugin development.
See [plugin development](03-plugin-development.md) for procedures and [Core modules](04-core-modules.md) for implementation ownership.

## 📌 Basic types

```python
Segments = list[dict[str, Any]]
Event = dict[str, Any]
```

OneBot message-segment structure:

```python
{
    "type": "text",
    "data": {"text": "你好"},
}
```

## 📌 `core.plugin_base`

### Message-segment construction

| Function | Returned segment | Purpose |
|---|---|---|
| `text(content)` | `text` | Text |
| `image(file_path)` | `image` | Local image, with path converted to a `file://` URI |
| `emoji(file_path, summary="")` | `emoji` | Local emoji image and optional summary |
| `image_url(url)` | `image` | Remote image URL |
| `face(face_id)` | `face` | QQ face |
| `record(file_path)` | `record` | Local audio |
| `record_url(url)` | `record` | Remote audio URL |
| `segments(payload)` | `Segments` | Normalize a string, list, or empty value |

Example:

```python
from core.plugin_base import image, segments, text

reply = [
    text("结果如下"),
    image("data/example/result.png"),
]

plain_reply = segments("完成")
```

### Action construction

```python
build_action(
    segs: Segments,
    user_id: int | None,
    group_id: int | None,
) -> dict[str, Any] | None
```

With `group_id`, this function creates `send_group_msg`. Other user-scoped calls create `send_private_msg`.
Empty segments return an empty action.

### Text boundaries

```python
has_control_characters(
    value: str,
    *,
    allow_formatting_whitespace: bool = False,
    include_c1: bool = False,
) -> bool
```

```python
bounded_external_text(
    value: object,
    *,
    max_chars: int,
    max_bytes: int,
    default: str = "",
    suffix: str = "…",
    strip_ansi: bool = True,
    strip: bool = True,
    truncate: bool = True,
) -> str
```

```python
head_tail_preview(text: str, max_chars: int, *, marker: str) -> str
```

These functions apply character, byte, and control-character limits to third-party responses, terminal output, titles, and log summaries.

```python
file_uri_to_path(value: str) -> Path | None
```

This function converts message-segment `file://` URIs to local paths.
It supports `file:///C:/x`, `file://C:/x`, `file://localhost/x`, and UNC.
Other URI types return `None`. Core handles URI generation for `image()` and `record()`.

### External commands

```python
from core.process_tree import ProcessResult, run_process

result = await run_process(argv, timeout=30, cwd=workdir, max_capture_bytes=64 * 1024)
```

Commands run in independent process trees. Windows uses Job objects. POSIX uses process groups.
Standard output and standard error share a capture budget.

Timeout, budget exhaustion, or caller cancellation removes the complete process tree.
Cleanup also removes background children left after the command exits. These children stop during cleanup.
`ProcessResult` contains `exit_code`, `stdout`, `stderr`, `timed_out`, and `output_limited`.

`exit_code` is `None` when Core terminates the process.

### Asynchronous utilities

```python
await run_sync(func, *args, **kwargs)
```

`run_sync()` submits a synchronous function to the current plugin's execution bulkhead.

```python
await gather_bounded(awaitables, *, limit: int)
```

This returns results in input order and limits the number of concurrent awaitables.

### File utilities

```python
ensure_dir(path: Path) -> None
atomic_write_bytes(path: Path, payload: bytes) -> None
atomic_write_text(path: Path, payload: str) -> None
load_json(path: Path, default=None, *, raise_on_error=False)
write_json(path: Path, data: Any) -> None
```

`atomic_write_bytes()` commits bytes through a temporary file in the same directory, `fsync`, and atomic replacement.
`atomic_write_text()` uses UTF-8 with the same primitive.
`load_json()` and `write_json()` use `AtomicJsonStore`.
Persistent data paths use `context.data_dir` as their root.

### Long-message splitting

```python
split_message_segments(
    segs: Segments,
    max_length: int = MAX_MESSAGE_TEXT_LENGTH,
) -> list[Segments]
```

This function splits long messages at text boundaries and keeps mixed-segment order.

<a id="plugincontext"></a>

## 🧩 `PluginContext`

`core.interfaces.PluginContextProtocol` defines the public structure.

### Identity and path properties

| Property | Type | Description |
|---|---|---|
| `plugin_name` | `str` | Current canonical plugin name |
| `plugin_dir` | `Path` | Current plugin source root |
| `data_dir` | `Path` | Current plugin persistent-data directory |
| `current_user_id` | `int \| None` | Current user |
| `current_group_id` | `int \| None` | Current group |
| `request_id` | `str \| None` | Current request tracking ID |
| `principal` | `PluginPrincipal` | Current caller identity |
| `command_invocation` | `CommandInvocation \| None` | Command node parsed by Core |
| `state` | `dict[str, Any]` | Shared memory state for the current plugin generation |

### Shared service properties

| Property | Type | Description |
|---|---|---|
| `logger` | logger adapter | Plugin logger that automatically attaches the request ID |
| `http_session` | `aiohttp.ClientSession \| None` | Shared HTTP session owned by the application |
| `send_action` | async callable | Send a proactive OneBot action and return its delivery result |
| `metrics` | `MetricsCollector \| None` | Runtime metrics |
| `capabilities` | `PluginCapabilities` | Capabilities issued from manifest declarations and identity |

The application lifecycle manages the shared HTTP session. Plugins reuse it within request scope.

```python
await context.send_action(action: dict[str, Any]) -> bool | None
```

`True` means all split actions have confirmed handoff to the delivery boundary.
`False` means at least one action has confirmed failure.
`None` means the transport might have submitted an action and the final result is unknown.
For unknown results, the business layer keeps pending state. This prevents duplicate messages from automatic retries.

Mark messages with credentials, such as login codes or tokens, as sensitive before delivery:

```python
from core.delivery import SENSITIVE_ACTION_KEY

action = build_action(segments(message), user_id, None)
action[SENSITIVE_ACTION_KEY] = True
await context.send_action(action)
```

Marked actions bypass `core.observe_outgoing_action` observers, including chat-context recording.
The delivery layer removes this marker and internal receipt fields before serialization.
Pendo Web login codes and widget tokens use this marker.

### Configuration methods

```python
context.get_config(path: str) -> Any
context.get_secret(path: str) -> Any
context.get_settings_snapshot() -> PluginSettingsSnapshot
context.now() -> datetime
context.default_groups() -> list[int]
```

`get_config()` and `get_secret()` accept dotted paths in the current plugin namespace.
`get_settings_snapshot()` returns read-only configuration and secret views from one revision.

### `PluginSettingsSnapshot`

| Property or method | Type | Description |
|---|---|---|
| `config` | `Mapping[str, Any]` | Public configuration snapshot for the current revision |
| `secrets` | `Mapping[str, Any]` | Secret configuration snapshot for the current revision |
| `revision` | `int` | Atomic publication version |
| `config_status` | `str` | Public configuration source state |
| `secrets_status` | `str` | Secret configuration source state |
| `plugin_config(plugin_name)` | `Mapping[str, Any]` | Specified plugin's public namespace |
| `plugin_secrets(plugin_name)` | `Mapping[str, Any]` | Specified plugin's secret namespace |

The context limits snapshots to the current plugin's visible scope.
Reuse one snapshot in a business transaction to keep public configuration and credentials on the same revision.

### `PluginPrincipal`

| Property | Type | Description |
|---|---|---|
| `kind` | `Literal["user", "scheduled_system", "lifecycle"]` | Caller type |
| `user_id` | `int \| None` | User principal QQ ID |
| `group_id` | `int \| None` | User principal's group |
| `is_bot_admin` | `bool` | Bot administrator state at issuance |
| `is_private` | `bool` | User principal came from private chat |
| `group_role` | `Literal["owner", "admin", "member", "unknown"]` | Current group role |
| `delivery_targets` | `tuple[DeliveryTarget, ...]` | Proactive delivery targets validated by Core |
| `schedule_delivery` | `ScheduleDeliveryMode \| None` | Core delivery mode for a scheduled principal |

`DeliveryTarget.kind` is `private` or `group`. `target_id` is a positive integer.
`principal.is_system` identifies scheduled principals.
`principal.can_manage_group(group_id)` checks whether the current user can manage that group.

### Mute methods

```python
context.mute_group(group_id: int, duration_minutes: float) -> None
context.unmute_group(group_id: int) -> bool
context.is_group_muted(group_id: int) -> bool
context.get_mute_remaining(group_id: int) -> float
```

`get_mute_remaining()` returns remaining mute time in minutes.

### Application operations

```python
context.reload_config() -> None
context.reload_plugins() -> asyncio.Task[None] | None
context.get_command_catalog() -> tuple[CommandCatalogNode, ...]
context.list_plugins() -> list[str]
```

These are in-process APIs for trusted plugins. Loaded plugins can call them directly.
Bot Core user commands are authorized per their own manifest declarations; Dispatcher checks them with `command_access` before the call.
`/help` and `/plugins` are public. `/reload` and `/metrics` need a Bot administrator and work in private and group chats. Mute commands need a Bot administrator and only work in groups. `/set_secret` and `/get_secret` need a Bot administrator and only work in private chats. See the [command entry index](09-plugins.md#️-command-entry-index).
Deployers protect code-call boundaries through plugin source review.

## 🧩 Capability

`context.capabilities` contains narrow Core-issued capabilities:

| Property | Purpose |
|---|---|
| `is_bot_admin` | Current principal's Bot administrator state for object-level rules. Manifest declarations and Core decide command permissions. |
| `is_system` | Scheduled-call state |
| `secret_admin` | Bot Core secret management. It follows `/set_secret` and checks each call. |
| `onebot_media` | XiaoQing Chat OneBot media queries |
| `config_subscription` | Pendo configuration-revision subscription |
| `codex_arxiv_summary` | arXiv Filter to Codex summary service. Scheduled calls are available. User calls follow `/codex`. |
| `voice_synthesis` | Smalltalk to Voice synthesis service |
| `chat_reply` | Smalltalk to Chat reply service |
| `agent_turn` | XiaoQing Chat turn handoff to XiaoQing Agent for user messages. Decision and execution can be separate. Reports current-reply delivery. |
| `codex_agent` | XiaoQing Agent synchronous Codex task. It follows `/codex` and checks each call. |
| `ssh_agent` | XiaoQing Agent QingSSH server list and one-time commands. It follows `/ssh` and checks each call. |
| `command_runner` | Execute published commands as the initiator with normal permission and scene checks. `run(text, *, sender_name, operation_id)` returns original `DeliverySegments`. The caller settles receipts. |
| `local_tools` | Authorize XiaoQing Agent local code execution and file operations. It follows `/shell` and checks each call. |
| `ai` | Common AI routes for the current plugin |

`is_bot_admin` and `is_system` always return booleans.
Application-issued contexts always supply the `ai` service.
Actual calls depend on the current plugin's routes, providers, models, and credentials.
Other optional services depend on manifest `capabilities`, `uses_services`, service bindings, and the current principal.

Their values are `None` when conditions remain unmet.
The protocol declares service properties optional. Minimal context implementations and test doubles can thus express unavailable services.

### AI Capability

```python
await context.capabilities.ai.complete(
    route: str,
    messages: list[dict[str, Any]],
    *,
    required_modalities: tuple[str, ...] = ("text",),
    pinned_model: str | None = None,
    temperature: float | None = None,
    top_p: float | None = None,
    max_tokens: int | None = None,
    timeout_seconds: float | None = None,
    total_timeout_seconds: float | None = None,
    max_retry: int | None = None,
    retry_interval_seconds: float | None = None,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: Any = None,
    extra_payload: Mapping[str, Any] | None = None,
) -> AICompletionResult
```

```python
context.capabilities.ai.list_models(
    route: str,
    *,
    required_modalities: tuple[str, ...] = ("text",),
) -> tuple[AIModelInfo, ...]
```

`list_models()` returns models with usable keys in route order. Missing keys skip a provider; a completely unavailable chain raises `AIConfigError`. `complete()` rejects a pinned model with a missing key. Main routes own routine request parameters; explicit call parameters support dynamic task budgets.

### `AIModelInfo`

| Property | Type | Description |
|---|---|---|
| `name` | `str` | Model profile name in project configuration |
| `provider` | `str` | Provider name |
| `model` | `str` | Model name sent to the provider |
| `modalities` | `tuple[str, ...]` | Declared input modalities |

### `AICompletionResult`

| Property | Type | Description |
|---|---|---|
| `response` | `dict[str, Any]` | Normalized provider response |
| `profile` | `str` | Successful model profile |
| `provider` | `str` | Successful provider |
| `model` | `str` | Successful model name |
| `finish_reason` | `str` | Completion termination reason |
| `attempts` | `int` | Total request attempts in the current model chain |
| `content` | `str` | Common Chat Completions text content |

Read `response` for complex responses. Prefer `content` for plain text calls.

## ⌨️ Command catalog types

### `CommandCatalogNode`

Core generates the command catalog from validated manifests.
It supplies immutable snapshots for help, export, permission checks, and plugin calls.

| Property | Type | Description |
|---|---|---|
| `code` | `str` | Stable command code |
| `plugin` | `str` | Owner's canonical plugin name |
| `path` | `tuple[str, ...]` | Canonical path from root command to this node |
| `name` | `str` | Current node's canonical name |
| `aliases` | `tuple[str, ...]` | Current node aliases |
| `help_text` | `str` | Short help |
| `usage` | `str` | Complete usage |
| `match_mode` | `str` | `prefix` or `exact` |
| `permission` | `str` | `public`, `bot_admin`, or `group_admin` |
| `contexts` | `tuple[str, ...]` | Set of `private` and `group` scenes |
| `examples` | `tuple[str, ...]` | Valid examples |
| `invalid_examples` | `tuple[str, ...]` | Invalid examples |
| `children` | `tuple[CommandCatalogNode, ...]` | Direct subcommands |

```python
node.walk() -> tuple[CommandCatalogNode, ...]
node.resolve_child(token: str) -> CommandCatalogNode | None
node.to_dict() -> dict[str, Any]
```

`walk()` returns this node and all descendants in catalog order.
`resolve_child()` resolves a direct child by canonical name or alias.
`to_dict()` returns the public catalog in a JSON-serializable structure.

### `CommandInvocation`

| Property or method | Type | Description |
|---|---|---|
| `root` | `CommandCatalogNode` | Top-level command node |
| `chain` | `tuple[CommandCatalogNode, ...]` | Matched node chain |
| `remainders` | `tuple[str, ...]` | Original remainder after consumption at each path depth |
| `node` | `CommandCatalogNode` | Deepest matched node |
| `arguments` | `str` | Business arguments after the deepest node |
| `remainder_after(depth)` | `str` | Remainder after the specified path depth. Root depth is 0. |

### `command_access()`

```python
from core.router import command_access

command_access(node, principal, *, is_bot_admin=None) -> str | None
```

This is the project-wide command permission function.
It uses node `permission` and `contexts` to decide access in the current private chat or group.
Allowed access returns `None`.
Denied permission returns `"bot_admin"` or `"group_admin"`.

Allowed permission with a prohibited scene returns `"context"`.
The function checks permission before scene.
An explicit `is_bot_admin` overrides the issuance snapshot, so long tasks can check current configuration.
Command execution, session continuation, Core-issued derived capabilities, and the agent command list all use this function.

## 📌 Send receipts (`core.delivery`)

| Interface | Description |
|---|---|
| `DeliveryReceipt(expected_actions, commit, rollback, unknown)` | Receipt for one logical reply. All physical acknowledgements trigger `commit`. Definite failure triggers `rollback`. Unknown results trigger `unknown`. Settlement occurs at most once. |
| `receipt.record(delivered)` | Record one transport result: `True`, `False`, or `None` for unknown. |
| `receipt.message_ids` | Confirmed physical message IDs in send order, including every split-reply part. Read these in the commit callback. |
| `receipt.resolved` / `committed` / `outcome` | Settlement state, commit-callback success, final three-state result |
| `DeliverySegments(values, receipt)` | Segments with a receipt. Plugin entries return them. Core settles the receipt with actual delivery. |
| `send_with_receipt(send_action, action, receipt)` | Proactively send one action with a receipt and settle its result exactly once. |

## 🌐 Session API

### Context methods

```python
await context.create_session(
    initial_data: dict[str, Any] | None = None,
    timeout: float | None = None,
) -> Session
```

```python
await context.get_session() -> Session | None
await context.update_session(callback: Callable[[Session], Any]) -> Any
await context.end_session() -> bool
await context.has_session() -> bool
```

`get_session()` returns an isolated snapshot and refreshes the idle timeout.
`update_session()` gives its callback a private working copy.
Successful return commits that copy. Exceptions and cancellation roll back the transaction.
`has_session()` queries active state.

Expiry ends a session. The plugin can later call `create_session()` to start a new session.

### `Session` properties

| Property | Type | Description |
|---|---|---|
| `user_id` | `int` | User ID |
| `group_id` | `int \| None` | Group ID |
| `plugin_name` | `str` | Session plugin |
| `session_id` | `str` | Unique session ID |
| `state` | `str` | Business state |
| `data` | `dict[str, Any]` | Business data |
| `created_at` | `float` | Creation timestamp |
| `updated_at` | `float` | Update timestamp |
| `timeout` | `float` | Idle timeout in seconds |
| `version` | `int` | Local version count |

### `Session` methods

```python
session.get(key, default=None) -> Any
session.set(key, value) -> None
session.delete(key) -> bool
session.clear() -> None
session.update() -> None
session.is_expired() -> bool
```

Make persistent changes in the working copy supplied to `context.update_session(callback)`.

<a id="argument-parsing"></a>

## 📌 Argument parsing

```python
from core.args import parse, parse_int, tokenize
```

### `parse()`

```python
args = parse('alpha "two words" --limit 5 -v')

args.tokens  # ["alpha", "two words"]
args.first  # "alpha"
args.second  # "two words"
args.get(0)  # "alpha"
args.rest(1)  # "two words"
args.opt("limit")  # "5"
args.has("v")  # True
```

This supports single-letter short options, ASCII long options, `--key=value`, and the `--` option terminator.

### `parse_int()`

```python
parse_int(text, *, minimum=None, maximum=None) -> int | None
```

This function accepts ASCII decimal integer strings and applies optional range limits.

## ⌨️ Plugin entry signatures

The following examples use recommended asynchronous definitions.
Core also accepts synchronous `def` callbacks with the same parameter signatures.
Synchronous callbacks enter the current plugin's synchronous bulkhead.
When a callback returns an awaitable, Core awaits its completion.

### Command

```python
async def handle(
    command: str,
    args: str,
    event: Event,
    context: PluginContextProtocol,
) -> Segments | list[dict[str, Any]] | str | None: ...
```

All entries can return `DeliverySegments` with receipts as message segments.

### Session

```python
async def handle_session(
    text: str,
    event: Event,
    context: PluginContextProtocol,
    session: Session,
) -> Segments | list[dict[str, Any]] | str | None: ...
```

### Smalltalk

```python
async def handle_smalltalk(
    text: str,
    event: Event,
    context: PluginContextProtocol,
) -> Segments | list[dict[str, Any]] | str | None: ...
```

### URL

```python
async def handle_url(
    url: str,
    event: Event,
    context: PluginContextProtocol,
) -> Segments | list[dict[str, Any]] | str | None: ...
```

### Lifecycle

```python
async def init(context: PluginContextProtocol) -> None: ...


async def shutdown(context: PluginContextProtocol) -> None: ...
```

### Schedule

```python
async def scheduled_handler(
    context: PluginContextProtocol,
) -> Segments | ScheduledDelivery | list[ScheduledDelivery] | str | None: ...
```

Manifest `delivery` determines the return type.
`broadcast` uses ordinary segments or strings.
`targeted` uses one or more `ScheduledDelivery` objects. `silent` returns `None`.
Core validates targets, constructs OneBot actions, and sends them.

`targeted` handlers construct target messages with Core types:

```python
ScheduledDelivery.group(group_id, message_segments, receipt=None)
ScheduledDelivery.private(user_id, message_segments, receipt=None)
```

Group targets must belong to the current schedule's Core-resolved `group_ids`.
Private targets must be positive integer QQ IDs.
Optional `receipt` commits business state after the OneBot delivery result becomes known.

## 🌐 OneBot event fields

Common plugin fields:

```python
event.get("post_type")
event.get("message_type")
event.get("user_id")
event.get("group_id")
event.get("message_id")
event.get("message")
event.get("raw_message")
event.get("sender")
```

Core normalizes `event["message"]` into message segments at entry.
Plugins can use `core.message` utilities to extract text and media segments.
`sender_display_name(event["sender"])` selects the first nonempty group card, nickname, or name, in that order.

## 🌐 Inbound API

### `POST /event`

- Request body: OneBot v11 event JSON object, with media type `application/json` or `application/*+json`.
- Authentication: `Authorization: Bearer <inbound_token>`.
- Standard success response: `{}`. The application sends replies through the OneBot action API and waits for acknowledgement.
- Custom action-list response: send `X-XiaoQing-Response-Mode: actions` explicitly to receive `{"actions": [...]}`.
  The caller executes the actions. The command matrix uses this mode.
- Main statuses: `200` success, `400` invalid JSON or event structure, `401` authentication failure, `415` wrong media type.
  `500` indicates a handler exception. `503` indicates shutdown or admission-queue overload.

### `WebSocket /ws`

- Input: OneBot v11 event JSON.
- Output: OneBot action JSON.
- Authentication: the same token policy as HTTP inbound.

### `GET /health`

This returns service state, runtime version, uptime, request count, and WebSocket connection count.
It adds plugin, session, and task counts when available.

### `GET /metrics`

This returns message, plugin, error, latency, and queue metrics.
A missing metrics provider returns `501`. Metrics-generation failure returns `500`.

`/health`, `/metrics`, `POST /event`, and WebSocket handshakes share one Bearer token.
Use a nonempty `inbound_token` when a listener is enabled.

## 🌐 OneBot actions

Group message:

```python
{
    "action": "send_group_msg",
    "params": {
        "group_id": 123456789,
        "message": [{"type": "text", "data": {"text": "你好"}}],
    },
}
```

Private message:

```python
{
    "action": "send_private_msg",
    "params": {
        "user_id": 123456789,
        "message": [{"type": "text", "data": {"text": "你好"}}],
    },
}
```

## 🧭 Next steps

- Manifest and development procedure: [plugin development](03-plugin-development.md)
- Configuration fields: [configuration](06-configuration.md)
- Runtime modes: [advanced topics](07-advanced.md)
