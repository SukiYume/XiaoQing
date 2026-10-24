# 🤖 Codex

[中文](README.md) | English

`codex` manages local Codex CLI background tasks through private or group QQ chats.
Results return to the chat that started the task.
Each session label has its own working directory, Codex thread, task queue, and conversation history.
Tasks within a label run sequentially. Different labels can run concurrently.

## 🔐 Permissions and execution scope

The Manifest sets `/codex` to `admin_only: true`. Private and group chats are supported.
All group members can see group task progress and results.
`admin_user_ids` in `config/secrets.json` identifies authorized users.

Codex CLI starts under the Bot process identity.
The Bot account, `sandbox`, `approval_policy`, and working directory determine filesystem permissions together.
`danger-full-access` grants CLI access to the files accessible to the Bot account.
Production configuration should use controlled `allowed_cwd_roots`, trusted administrators, and dedicated working directories.

## ⌨️ Commands

| Function | Primary command | Manifest aliases |
| --- | --- | --- |
| Codex session queue | `/codex` | This entry only |

| Usage | Description |
| --- | --- |
| `/codex create <name> [cwd:<path>]` | Create a session |
| `/codex <name> <task>` | Submit a task |
| `/codex list` | List sessions |
| `/codex status [name]` | Show all sessions or one session |
| `/codex cancel <name> [job_id]` | Cancel a running task or remove a queued task |
| `/codex clear <name>` | Remove all queued tasks for a session |
| `/codex delete <name> [--force] [--protected]` | Delete a session and archive history |
| `/codex help` | Show local help |

Subcommand aliases:

- `create`: `new`, `创建`.
- `list`: `ls`, `列表`.
- `status`: `状态`.
- `cancel`: `stop`, `取消`, `停止`.
- `clear`: `清空`.
- `delete`: `del`, `remove`, `rm`, `删除`.
- `help`: `帮助`, `?`.

Windows paths should use `/`, for example `C:/workspace/project`.
Linux and macOS use their corresponding absolute paths.

## ⚙️ Basic configuration

The settings are in `config/config.json`:

```json
{
  "plugins": {
    "codex": {
      "codex_bin": "codex",
      "default_cwd": "C:/workspace",
      "allowed_cwd_roots": ["C:/workspace"],
      "sandbox": "workspace-write",
      "approval_policy": "never",
      "skip_git_repo_check": true,
      "max_parallel_jobs": 2,
      "per_session_queue_limit": 10,
      "job_timeout_seconds": 3600,
      "protected_sessions": ["astro-ph"],
      "arxiv_summary": {
        "label": "astro-ph",
        "cwd": "C:/workspace/arxiv",
        "methodology": "arxiv-summary-methodology.md"
      }
    }
  }
}
```

When `default_cwd` is omitted, the plugin uses `data/codex/workspaces/`.
When `allowed_cwd_roots` is omitted, the list contains the default working directory.
`sandbox` accepts `read-only`, `workspace-write`, and `danger-full-access`.
`approval_policy` accepts `untrusted`, `on-failure`, `on-request`, and `never`.

## 📌 Resource limits

The configuration reader examines field types and constrains integers to safe ranges defined in code.
Main default values follow:

| Category | Fields and defaults | Purpose |
| --- | --- | --- |
| Queue | `max_parallel_jobs=2`, `per_session_queue_limit=10`, `emergency_queue_limit=1000` | Global concurrency and session queue size |
| Prompt | `max_prompt_chars=200000` | User-task and internal-task prompt length |
| Process | `spawn_timeout_seconds=30`, `job_timeout_seconds=3600` | CLI creation and task execution time |
| Output | `max_stdout_bytes=16777216`, `max_stderr_bytes=4194304`, `max_json_line_bytes=1048576` | Output streams and individual JSON events |
| Results | `max_final_output_bytes=8388608`, `max_qq_text_chars=60000` | Final-result archive and QQ text preview |
| Scan | `artifact_scan_max_entries=5000`, `artifact_scan_max_depth=8` | Job artifact directory traversal |
| Images | `max_image_artifacts=20`, `max_image_bytes=20971520`, `max_image_total_bytes=104857600` | Candidate image count and bytes |
| Decode | `max_image_pixels=40000000`, `max_image_frames=120`, `max_qq_images=10` | Image decode complexity and QQ delivery count |
| Retention | `session_ttl_days=90`, `artifact_retention_days=30` | Expired-session archives and task artifact cleanup |
| Disk | `emergency_disk_bytes=10737418240` | Emergency data-directory capacity threshold |

When an output stream or final output reaches a hard limit, the plugin terminates the task and saves a record within limits.
When QQ text reaches the preview limit, the complete result goes to a local archive.
QQ receives truncated content and the archive location.
Image checks cover path ownership, file identity, format, decoding, bytes, pixels, and frames.

## 💬 arXiv summary session

`arxiv_filter` passes the day's positive papers through `codex.enqueue_arxiv_summary` to a fixed session.
The default label is `astro-ph`.
Input dates must be valid `YYYY-MM-DD` values. Each call accepts up to 512 `arxiv.org/abs` or `arxiv.org/pdf` links.

The plugin normalizes versions, PDF suffixes, and query parameters to HTTPS abs links.
Task identity combines the source-list date and normalized link set.

- A successful result causes the historical summary to be sent again.
- A queued or running task returns its current status.
- A new link set on the same date creates a new task.
- A failed record permits a new summary task.

At first use, the plugin submits an internal initialization task to establish summary rules.
It then submits the day's summary.
For each summary task, Codex must read the file specified by `arxiv_summary.methodology`.
`astro-ph` automatically joins the protected-session list. This command deletes it:

```text
/codex delete astro-ph --force --protected
```

## 🛠 XiaoQing agent tasks

`xiaoqing_agent` submits coding tasks through `codex.agent_task` and waits for completion.
Core issues the service according to the `/codex` declaration in `plugin.json`.
The caller must have permission to use `/codex`. Bot administrator status is necessary for this permission.

- Each QQ chat uses a Codex session named `agent-<session>`. Later tasks reuse the same thread.
- A missing session uses the requested `cwd` within `allowed_cwd_roots`, or the default working directory.
- Tasks carry `suppress_delivery`. Result text and image artifact paths return to the agent for its reply.
- Codex delivery is suppressed for these tasks.
- If the agent wait times out or stops, queued tasks leave the queue.
- Running tasks use the common process-tree cancellation procedure.

These sessions appear in `/codex list`. `/codex status` and `/codex cancel` manage them.

## 💾 Runtime data

The default data root is `data/codex/`:

| Path | Content |
| --- | --- |
| `sessions.json` | Labels, working directories, owners, thread IDs, and task counts |
| `quarantine/sessions-*.json` | State snapshots that failed validation |
| `session/<name>/conversation.jsonl` | User tasks, Codex replies, and task-status events |
| `session/<name>/images/` | Archived and delivered images |
| `session/<name>/jobs/job-<id>/artifacts/` | Artifacts for one task |
| `deleted_sessions/<name>-<time>/` | History after session deletion |
| `outputs/` | Temporary Codex CLI output |

State files use a versioned field allowlist.
The loader examines the syntax, structure, fields, and paths of the main file and then `.bak`.
An invalid main file is restored from a valid backup.
If both copies are unusable, their snapshots enter `quarantine/`, and runtime starts with safe state.
Session deletion moves the history directory. A recreated session with the same name starts a new record chain.

## 🎨 Image results

The plugin appends image-output instructions to task prompts.
Codex can save images in the current task's `artifacts/` directory.
Final text can also explicitly reference local files with Markdown image syntax or `图片: <path>`.

Delivery examines current-task artifacts and explicit paths in final text only.
Validated images are copied to the session image directory and sent as OneBot image segments.

## ⏰ Lifecycle

The first command or arXiv service call creates a process-local manager.
Configuration generation changes update concurrency and resource limits.
At unload, reload, or Bot shutdown, `shutdown()` stops task acceptance, cancels running process trees, waits for workers, and saves state.

## 🩺 Troubleshooting

| Symptom | Check |
| --- | --- |
| Session creation failure | Examine absolute paths, `allowed_cwd_roots`, and Bot account directory permissions. |
| CLI creation failure | Examine `codex_bin`, PATH, and `spawn_timeout_seconds`. |
| Task remains queued | Examine active tasks for the label, concurrency, and queue limits. |
| Timeout or output limit | Examine task status, runtime logs, and the applicable resource limit. |
| Images appear only as text | Examine artifact directories, explicit paths, formats, and image limits. |
| arXiv methodology read failure | Examine `arxiv_summary.cwd` and `methodology`. |

## ✅ Development checks

Run these commands from the repository root:

```bash
python -m pytest -q tests/plugins/codex
python -m ruff check plugins/codex
python -m mypy plugins/codex
```
