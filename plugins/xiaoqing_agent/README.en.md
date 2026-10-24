# 🛠 XiaoQing Agent

[中文](README.md) | English

XiaoQing Agent does tasks when a user asks XiaoQing for help. It can query events, tasks, papers, and astronomy information. It can calculate, plot, execute code and commands, inspect servers, and give programming tasks to Codex. It replies with XiaoQing's voice after completion. `xiaoqing_chat` handles standard conversation.

## 🔐 Permissions

The agent has the same command permissions as the initiating user. Each plugin declares access in `plugin.json`. Core checks access through `core.router.command_access`. Each agent capability follows the declaration for its related command.

| Capability | Users | Existing permission |
| --- | --- | --- |
| XiaoQing commands (`xiaoqing_command`) | All users | Execute with the initiator identity. Core checks each command's public, group-administrator, or Bot-administrator permission and private/group context. |
| Python, Shell, and local file reads and writes | Bot administrators | Core issues `local_tools` from the `/shell` declaration in `plugin.json`. |
| SSH (`ssh_servers`, `ssh_run`) | Bot administrators | Core issues `qingssh.agent_command` from the `/ssh` declaration. |
| Codex (`codex_run`) | Bot administrators | Core issues `codex.agent_task` from the `/codex` declaration. |

- An explicit call can use private chat, an @ mention, XiaoQing's name, or a quoted XiaoQing message. The agent first decides whether the message requests work. Each turn checks Bot administrator status against current `secrets.admin_user_ids`.
- Each local, SSH, and Codex call checks Core permissions again. Removing administrator status during a task causes the next call to fail. Permitted file delivery then narrows to outputs from that task.
- The model sees only tools and commands available to the initiator. Standard member tasks have XiaoQing commands and their returned files. Bot administrator status is necessary for local Python, Shell, file access, SSH, and Codex. XiaoQing explains that requirement when necessary.
- Initiators with `/shell`, `/ssh`, and `/codex` access can use all tools. Bot administrator status is necessary for these commands. Python and Shell run directly as the Bot operating-system account. Agent execution bypasses the `/shell` command enable list. SSH inherits the server account permissions. Codex follows `plugins.codex` sandbox and directory settings.
- Only the initiator and Bot administrators can stop a running task or add instructions. Other callers receive a task-ownership message.
- Only the group initiator's own messages are instructions. Other members' messages and quoted messages enter model context with speaker labels.
- Group task results return to that group. Use private chat for sensitive tasks. Commands such as `/pendo` operate only in private chat.

Everyone can use `/agent`. The same rules apply.

## ⚙️ Enable the agent

Enable the plugin in `config/config.json`. Configure a model route that supports tool calls:

```json
{
  "plugins": {
    "xiaoqing_agent": {
      "enabled": true,
      "ai": {
        "routes": {
          "agent": {
            "models": ["deepseek-pro", "deepseek-flash"],
            "timeout_seconds": 180,
            "total_timeout_seconds": 400,
            "max_retry": 1
          },
          "triage": {
            "models": ["deepseek-flash"],
            "timeout_seconds": 15,
            "total_timeout_seconds": 20
          }
        }
      }
    }
  }
}
```

- `enabled` defaults to `false`. When disabled, `xiaoqing_chat` uses its normal behavior. Configuration changes apply to the next message.
- The `agent` route executes tasks. OpenAI-compatible `tools` calls are necessary. Reasoning models, including DeepSeek reasoning mode, can be used directly.
- The `triage` route classifies conversation and work. A fast model with reasoning disabled is recommended. An omitted route uses `agent`.
- Normal operation also continues after removal of the complete `plugins/xiaoqing_agent/` directory. `xiaoqing_chat` returns to standard conversation when the service is unavailable.

All other fields are optional:

| Field | Default | Function |
| --- | --- | --- |
| `max_steps` | `40` | Maximum model turns per task. Range 1–200. |
| `task_timeout_seconds` | `1800` | Total time from task start to delivery. Range 30–86400. |
| `tool_timeout_seconds` | `120` | Default timeout for tools without a specified timeout. |
| `max_tool_timeout_seconds` | `900` | Longest timeout a model can request for one tool call. Range 5–7200. |
| `inline_wait_seconds` | `12` | Maximum wait in the current turn. Range 0–25. Completed results become the reply. Longer tasks continue in the background after an acknowledgment. |
| `max_admin_tasks` | `3` | Concurrent Bot administrator tasks. Range 1–20. |
| `max_member_tasks` | `4` | Total concurrent standard member tasks. Range 0–50. With `0`, only Bot administrators can start tasks. |
| `max_tasks_per_member` | `1` | Concurrent tasks per standard member across all sessions. Range 1–20. |
| `max_tool_output_chars` | `12000` | Maximum characters from each tool output supplied to the model. |
| `history_tasks` | `5` | Recent session tasks supplied for classification and execution. This supports requests to change previous results. |
| `python` | Bot interpreter | Interpreter for `run_python`. |
| `shell` | Automatic | Shell for `run_shell`. Windows prefers Git Bash, then PowerShell. Linux and macOS use bash. |

## 💬 Use

Tell XiaoQing the task directly:

```text
小青，2 的 100 次方是多少，各位数字加起来呢
帮我画一张 sin 和 cos 在 0 到 2π 的对比图
把刚才那张图的 sin 改成红色虚线
生成一个 1 到 10 平方的 csv 发给我
帮我在待办里加一条：明天下午三点交周报
登上 paris 看一下系统版本和运行了多久
做到哪了？
换成红色（任务还在跑时，补充要求会交给那个任务）
算了，别跑了
```

Each turn follows this process:

1. `xiaoqing_chat` completes invocation detection. It gives the agent the message, recent chat, attached images, downloaded image files, and quoted text and images. It also supplies XiaoQing's persona and speech rules. Chat reply generation starts at the same time.
2. The `triage` route classifies the message. Its context includes a catalog-generated list of commands available to the speaker. Requests for live data, personal data, existing functions, capabilities, or function usage count as work. A fast model typically takes about 1 second. Standard conversation uses the reply already being generated. Agent takeover cancels that generation and releases its session generation slot. Progress queries, stop requests, and additions to running tasks are handled directly. New work starts a task. Changes to completed results also start a new task.
3. Tasks execute in the background. Completion within `inline_wait_seconds` gives the result as the current reply. Longer tasks first send an acknowledgment. On completion, they send results to the original conversation. Group results mention the initiator first.
4. Text and images share one message. Other files use QQ file upload. Agent replies and background results enter chat history as XiaoQing messages with sent message IDs. Split replies record each ID. Very long recorded text is shortened. Quoting any recorded message lets XiaoQing identify a reply to her. Chat also reports current-reply delivery status to the task record.
5. Task selection is necessary for a stop request with multiple accessible tasks. XiaoQing lists those tasks. An invalid task ID returns a missing-task message.

### Speech style

The agent uses XiaoQing's established persona. The persona exists in `plugins/xiaoqing_chat/config/persona.md`. Each chat handoff supplies character facts and the `〔办事〕` sections. These sections define speech style, task replies, and examples. The agent uses this supplied content for classification acknowledgments and final replies.

Direct tasks such as `/agent run` use the most recently supplied persona. Before any handoff, they use natural conversational speech. Edit the persona card's `〔办事〕` sections and examples to change task speech.

Result replies also follow these rules:

- Use short conversational sentences suitable for QQ. A complete reply usually has at most five lines. Requested lists, such as weekly events, include all requested entries.
- Use plain text. Omit Markdown, `【】` headings, list markers, tables, code fences, and emoji category icons.
- Answer the requested question first. Include internal IDs, undo prompts, field names, and output icons only when requested.
- Describe a data change in one sentence with the affected record and new value. Include extra records created or deleted during retries. Query replies describe results. Keep replies focused on actions and results. Omit customer-service closings.
- Send images and files when requested or when the result itself is an image, table, or file.
- Use “我” for self-reference. Keep internal tool names and AI, assistant, or bot labels out of replies. Give the necessary command when the user must act.
- For capability questions, select examples from functions available to the speaker. Refer detailed questions to `/help`.

### Commands

| Function | Recommended entry | Manifest aliases |
| --- | --- | --- |
| Agent task management | `/agent` | This entry only |

| Command | Function |
| --- | --- |
| `/agent run <任务>` | Execute directly with classification omitted. Alias: `执行`. |
| `/agent status [任务ID]` | Show running tasks, plans, and current steps. Alias: `状态`. |
| `/agent stop [任务ID\|all]` | Stop initiated tasks. Bot administrators can stop any task. With no argument, exactly one accessible task is stopped. For multiple tasks, an ID or `all` is necessary. Aliases: `停止`, `cancel`. |
| `/agent history` | Show recent session tasks and mark failed result delivery. Alias: `历史`. |
| `/agent result <任务ID>` | Send a completed task's saved result and attachments again in this session. Alias: `结果`. |
| `/agent help` | Show usage. |

With an explicit task ID, Bot administrators can use `status` and `stop` across sessions. Other users see tasks in their current session.

### Sessions and group chat

- Session keys are `g<群号>` or `u<QQ号>`. One group shares its workspace, task records, and Codex session. Group members can read running tasks and history. Records, progress, and history identify the initiator. The initiator and Bot administrators can stop tasks or add requirements.
- Administrator and member capacities are separate. Administrators have `max_admin_tasks` slots. Standard members share `max_member_tasks` slots and have `max_tasks_per_member` slots each. Member tasks leave administrator capacity available. Full capacity returns a message immediately. Tasks are not queued.

## 🧰 Tools

| Tool | Function |
| --- | --- |
| `run_python` (Bot administrator) | Run Python in the workspace as a separate process. Matplotlib uses the Agg backend. |
| `run_shell` (Bot administrator) | Run a shell command in the workspace. |
| `read_file` / `write_file` (Bot administrator) | Read and write text files. |
| `update_plan` | Record a multi-step plan visible through `/agent status`. |
| `send_message` | Send progress during long tasks. Maximum: 8 messages per task, at least 15 seconds apart. |
| `wait` | Wait for polling. Maximum: 600 seconds per call. |
| `final_answer` | Deliver result text, images, and files. |
| `xiaoqing_commands` / `xiaoqing_command` | List and execute published XiaoQing commands, such as `/pendo`, with initiator permissions. |
| `ssh_servers` / `ssh_run` (Bot administrator) | List QingSSH servers and execute remote commands through single-use connections. |
| `codex_run` (Bot administrator) | Give a programming task to Codex CLI. Wait for text and image results. |

- Tool availability follows initiator permissions and current capabilities. Loaded SSH and Codex plugins are necessary for their tools. Bot administrator status is necessary for restricted tools.
- `xiaoqing_command` uses the command phase. Results match direct execution by the initiator. Group administrator commands use the current event's group identity. Display names use the original group card or nickname. `/agent` and the `/xc` chat entry reject these calls. Commands that start interactive sessions, such as guessing games or `/py repl`, have those sessions removed immediately. Private-only commands, such as `/pendo`, explain the private-chat requirement when called in groups.
- `[CQ:at,qq=QQ号]` inside commands becomes an @ segment, for example in pet gifts. Returned images, audio, video, and files expose local paths for forwarding. Text and images share one message. Audio and video each use separate messages. Other files use upload.
- Each `ssh_run` opens a connection and closes it after execution. `/ssh` interactive sessions remain available. Supported hosts include Linux, macOS, and Windows with OpenSSH and Git Bash.
- `codex_run` uses a Codex session named `agent-<会话>`. Later tasks in that session reuse the Codex thread. `allowed_cwd_roots` restricts its workspace. The agent presents results. Codex sends no additional result message.
- Long waits in `ssh_run` and `codex_run` occur outside QingSSH and Codex plugin execution lanes. Other users' `/ssh` and `/codex` commands continue during long remote tasks.
- `xiaoqing_command` uses a temporary session space. Existing initiator interactions, such as `/ssh`, stay separate and remain active. When a command waits for input, the plugin's `close_session` releases connections and other resources before discarding the temporary session. This matches user exit cleanup. Interactive `/ssh <服务器>` rejects agent calls. Use `ssh_run` for remote commands.
- Some commands return delivery receipts. For example, `/twimg` reserves selected images. Actual attachment delivery settles the receipt. Successful attachment delivery commits it. Failed delivery, text-only replies, task completion without delivery, and cancellation release the reservation. Later image requests can then use the image. Each invocation has a separate operation identifier. Separate calls in one task keep separate submission identities.

## ⏱ Runtime boundaries

- Each subprocess owns a process tree. Windows uses Job Objects. POSIX uses process groups. Timeout, output above 2 MB, task cancellation, or process shutdown removes the complete tree, including background child processes.
- `max_steps` and `task_timeout_seconds` limit tasks. At exhaustion, the model summarizes available results and remaining work with tools disabled.
- New requirements cancel tool calls in the current batch that have not started. The next step plans with the new requirements.
- Context length includes text, reasoning, and tool arguments. At the limit, older outputs, arguments, and reasoning shrink to initial previews. Recent content remains complete. Execution transcripts keep full exchanges.
- `read_file` reads ranges. Only the requested range enters memory.
- Classification model failure or an internal agent exception returns the turn to normal chat. A model failure after task start marks the task failed. The initiator receives an incomplete-task message.
- Delivery failure leaves execution completed and marks delivery failed. This includes background delivery, current replies, direct `/agent run` results, and attachment uploads. `/agent history` marks the failure. `/agent result <任务ID>` sends the saved result again and records delivery status. Saved text and attachments keep the original result. Plugin reload and process shutdown stop executing tasks. Completed tasks that are delivering results receive a short completion window.

## 🧩 Module structure

```text
xiaoqing_chat ──agent.handle_turn（decide / act）──► main.py ── triage.py（decision）
      ▲                                                  │
      └──────────── agent.report_delivery ◄──────────────┤
                                                         ▼
                                    runtime.py（capacity, registration, current/background delivery, stop, shutdown）
                                                         │
                                       loop.py（model <-> tools, added requirements, budgets, context compression）
                                                         │
                                       tools.py ─┬─ command_runner ─► Core execute commands as the initiator
                                                 ├─ local_tools ────► shells.py → core.process_tree
                                                 ├─ qingssh.agent_command ─► QingSSH
                                                 └─ codex.agent_task ──────► Codex
                                                         │
                         delivery.py（messages and attachments）· receipts.py（command receipt settlement）· tasks.py（records）
```

| Module | Function |
| --- | --- |
| `main.py` | `agent.handle_turn`, `agent.report_delivery`, and `/agent`. Separate decide and act phases let chat generation run in parallel. |
| `triage.py` | Classify conversation, new work, progress queries, stop requests, and added requirements through the `triage` route. |
| `prompts.py` | Classification and execution prompts, supplied persona, and reply rules. Filter command catalogs with the same permission check as Core. |
| `runtime.py` | Registration, separate administrator/member capacity, background execution, current or proactive delivery, delivery reports, resend, stop, and shutdown. |
| `loop.py` | Model/tool loop, real execution, added requirements, step and time budgets, context compression, and full transcripts. |
| `tools.py` | Tool definitions and execution. Build tools from initiator capabilities. Recheck Core before each local call. |
| `shells.py` | Select the configured shell or Windows Git Bash/PowerShell fallback. Share `core.process_tree` execution, tree cleanup, and output budgets with `/shell`. |
| `delivery.py` | Build QQ messages, combine text and images, send audio/video separately, upload files, and return per-item delivery results. |
| `receipts.py` | Settle command receipts on attachment delivery. Release undelivered receipts at task completion. |
| `tasks.py` | Task/session models, initiator identity, session task records, and transcripts. |
| `config.py` | Read `config.plugins.xiaoqing_agent`. Invalid types or out-of-range fields use defaults. |

Chat handoff is in `plugins/xiaoqing_chat/agent_bridge.py`. Core capability issuance is in `core/app_plugin_context.py`. The command executor is `run_command_text` in `core/dispatcher.py`.

## 📁 Data

```text
data/xiaoqing_agent/
├── workspaces/<会话>/          # One workspace per session. Keep files for later tasks.
│   ├── out/<任务ID>/           # Task outputs
│   └── .agent/scripts/         # Scripts executed by run_python
├── tasks/<会话>.json           # Latest 30 session tasks, including initiator and delivery state
└── transcripts/<任务ID>.json   # Full uncompressed model/tool exchanges. Keep the latest 200 tasks.
```

Each session workspace keeps files for later tasks. `out/<任务ID>/` holds task outputs. `.agent/scripts/` holds executed Python scripts. Session task files keep the latest 30 records, including initiator and delivery status. Transcripts keep full uncompressed model/tool exchanges for the latest 200 tasks.

After restart, unfinished tasks from the previous process become `interrupted`. Workspaces organize files. Bot administrator Python and Shell tasks can access every file available to the Bot operating-system account.

## 🧪 Tests

```bash
python -m pytest tests/plugins/xiaoqing_agent tests/core/test_app_agent_capabilities.py \
  tests/core/test_process_tree.py tests/plugins/xiaoqing_chat/test_xiaoqing_chat_agent_bridge.py \
  tests/plugins/codex/test_codex_agent_task.py tests/plugins/qingssh/test_qingssh_agent_command.py
```

Subprocess timeout, cancellation, and tree cleanup tests use real processes in `tests/core/test_process_tree.py`. Windows tests use a Proactor event loop.
