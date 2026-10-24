# 📦 09 - Plugin manual

[中文](09-plugins.md) | English

This chapter covers all 31 built-in plugins: purpose, main commands, permissions, configuration, data, and operation.
Runtime `/help` supplies the current recursive manifest command catalog.
Plugin READMEs describe specialized boundaries and maintenance details.
Command examples keep actual Chinese triggers, placeholders, and sample content.

## ⌨️ Use the help catalog

```text
/help
/help <插件名>
/help <命令路径>
/help <稳定命令 code>
/help search <关键词>
/help json <插件名>
```

The overview shows function areas and plugins. Plugin pages show top-level entries.
Branch pages show direct subcommands.
Leaf pages show complete usage, aliases, permissions, scenes, valid examples, and invalid examples.
All catalogs support `page N`.

```text
/help pendo
/help pendo todo
/help pendo todo add
/help minecraft
/help shell
```

`/plugins` shows plugins loaded in the current process.
Public settings are under `config.plugins.<plugin_name>`. Credentials are under `secrets.plugins.<plugin_name>`.
AI routes are under `config.plugins.<plugin_name>.ai.routes`.

## 📌 Core and personal management

### `bot_core`: Core management

Bot Core supplies hierarchical help, plugin lists, configuration and plugin reload, group mute, secret management, and runtime metrics.

**Main commands**

| Command | Scene and permission | Function |
|---|---|---|
| `/help [query] [page N]` | Public | Browse or search the command catalog. |
| `/help json [query] [page N]` | Public | Return a structured catalog. |
| `/plugins` | Public | Show loaded plugins. |
| `/reload` | Bot administrator | Reload configuration and plugins. Report result and duration after completion. |
| `/闭嘴 [duration]` | Bot administrator, group | Pause ordinary replies in this group. |
| `/说话` | Bot administrator, group | Resume ordinary replies in this group. |
| `/set_secret <path> <value>` | Bot administrator, private | Update an existing secret path. |
| `/get_secret <path>` | Bot administrator, private | Show a redacted value or object keys. |
| `/metrics` | Bot administrator | Show uptime, success rate, errors, and slow plugins. |

`/reload` first confirms configuration-revision publication and background plugin-reload startup.
After completion, it sends one success or failure result with duration to the original administrator conversation.
Mute defaults to 10 minutes. It supports minutes, hours, and Chinese units, up to 24 hours.

Secret paths use dotted names. Writes publish a new configuration revision.
Queries redact values according to type.
Complete, valid single-file secret candidates produce private administrator notifications with field paths.

Administrators check them and confirm with `/reload`.
Core observation services supply runtime metrics.

Core configuration, mute, metrics, and plugin-management services own data and lifecycle.
[Bot Core README](../plugins/bot_core/README.en.md) supplies all aliases, secret-value rules, and reload procedures.

### `pendo`: Personal time and information management

Pendo manages events, tasks, notes, journals, accounts, reminders, search, settings, Web, Scriptable widgets, and data import/export in private chat.
All business data is isolated by QQ user.

Ledger summaries calculate each currency separately.
Web statistics default to CNY and support specified currencies and `by_currency` groups.
Chat summaries, exports, and widgets keep currency labels.
Event and task inputs use the user's timezone.

Offset timestamps keep actual instants.
Reminder queues use UTC.
Complete edits submit the item version read earlier. Conflicts keep the user's edit and request a refresh.

**Quick entries**

```text
/pendo
/pendo help <event|todo|note|diary|ledger|search|settings|web>
/日程 ...
/待办 ...
/日记 ...
```

**Events**

```text
/pendo event add <内容>
/pendo event list [today|tomorrow|week|month|year|YYYY-MM|start..end]
/pendo event view <id>
/pendo event edit <id> <内容>
/pendo event delete <id>
/pendo event reminders <list|set|delete|confirm> ...
```

Collections organize recurring and multi-node events.
Collection IDs address the whole collection. Leaf IDs address one occurrence or node.

**Tasks**

```text
/pendo todo add [内容] [plan:YYYY-MM-DD] [deadline:YYYY-MM-DDTHH:MM]
/pendo todo list [today|open|done|cancelled|overdue|upcoming|inbox]
/pendo todo view <id>
/pendo todo done|cancel|undone <id>
/pendo todo edit <id> <字段>
/pendo todo delete <id|cat:分类>
```

An empty `todo add` starts a short session to collect content and a planned date.
One-line input can also include reminders, category, priority, and tags.

**Notes, journals, and accounts**

```text
/pendo note add|list|view|edit|append|tag|untag|link|delete ...
/pendo diary add|template|list|view|delete ...
/pendo ledger add|quick|list|view|edit|delete|summary ...
```

Notes support categories, tags, references, and related items.
Journals support multiple entries per day, template answers, weather, location, mood, score, and favorites.
The ledger supports income, expense, transfer, account, merchant, category, date, and amount ranges.
Statistics use integer minor-unit fields.

**Search, reminders, and settings**

```text
/pendo search <关键词> [type=] [range=] [status=] [category=]
/pendo confirm <id>
/pendo snooze <id> <10m|1h|19:00>
/pendo undo [1..5]
/pendo settings view
/pendo settings reminder on|off
/pendo settings timezone Asia/Shanghai
/pendo settings quiet_hours 23:00-07:00
/pendo settings daily_report 08:00
/pendo settings daily_briefing on|off
/pendo settings diary_remind 21:30
/pendo settings ai_consent on|off
```

Operation snapshots last 5 minutes.
`ai_consent` controls permission for external AI analysis of journal bodies.
Event and task reminders always use private messages to the item owner.
OneBot receipts complete or release reminder claims.
Item writes and edits update reminder queues together.
Schedule queries use canonical UTC trigger times with second precision.

Soft deletion keeps reminder history. Undo restores reminders with pending execution.
Recurrence `COUNT` constrains the entire original sequence.
Generation within a query window keeps the same count origin.

Backups read at JSON record boundaries. Chinese filenames use UTF-8 percent encoding.
Export scale and import-validation limits remain consistent.

Web event details permit early confirmation of each future reminder.
Users can enable it again at the same location after confirmation.
Buttons become disabled after the reminder is due.
State changes affect only the current user, current event, and specified reminder point.

**Web and data**

```text
/pendo web start|status|stop
/pendo web token
/pendo web widget-token
/pendo web widget-revoke
/pendo export ...
/pendo import
```

One-time login codes have a 7-day lifetime. Redeemed HttpOnly browser sessions also last 7 days.
Widget Bearer tokens last 365 days and permit only `/api/widget/*`.
Credential digests, browser sessions, widget registrations, and business tables are in `data/pendo/pendo.db`.

Scriptable home-screen summaries show at most 5 events.
Configure the Web URL and widget token at the script top.
Direct script execution uses a separate calendar interface with a complete window from at least 30 days ago to 30 days ahead.
An earlier successful-day cursor extends the window to fill the gap.
Synchronization makes one API query and one target-calendar query.
It adds, updates, and removes managed events in place by Pendo item ID.

Reminders, daily briefings, journal prompts, task rollover, and financial summaries use Core `targeted` private delivery to owners.
Operation-log and demo-data cleanup use `silent` mode.
`scheduled_delivery_outbox` records delivery acknowledgements per target.
See [Pendo README](../plugins/pendo/README.en.md), [architecture](../plugins/pendo/ARCHITECTURE.en.md), and [Scriptable guide](pendo-scriptable-widget.en.md) for complete time and field definitions.

## 🎨 Intelligent chat and voice

### `xiaoqing_chat`: Persona chat runtime

XiaoQing Chat lets the `bot_name` persona participate in groups and private chats as a conversational character.
It decides when and whom to answer, interprets images and memes, remembers conversations, and learns group expressions and jargon.
Each reply passes independent review before delivery.
It runs as the global smalltalk provider and also supplies explicit `/xc` access.

**Enable and command**

```json
{
  "bot_name": "小青",
  "plugins": {
    "smalltalk_provider": "xiaoqing_chat"
  }
}
```

```text
/xc <内容>
/xc help
/xc reset [confirm]
/xc stats
/xc brain
/xc config
/xc memory <关键词>
/xc expression
/xc jargon
/xc review <ok|no|answer|close> <会话ID> [内容]
/xc model [名称|default|global <名称|default>]
```

Private `/xc reset` clears the user's session.
Group `/xc reset confirm`, `/xc review`, and `/xc model` viewing or switching are for group owners, group administrators, and Bot administrators.
Private `/xc model` and `/xc model global` are for Bot administrators.
Model switching affects only the `chat` route and remains in process memory.
Restart restores configured models.

**Participation**

- Explicit invocation requests a reply: private chat, `/xc`, group mentions, persona-name text, or replies to persona messages.
  It also includes follow-up questions within 60 seconds after name-only invocation and pronouns referring to the persona in recent discussion.
- Ordinary group chat first passes hard limits: minimum interval, per-minute cap, consecutive-reply cooldown, low-information input, and initial mentions of other members.
  Then it uses a base-probability draw.
  Group invitations, image requests, open questions, cheering, excited sharing, repeated phrases, and active topics increase probability.
  Heartflow soft scores and consecutive silence supply further adjustment.
- The PFC planner then selects reply, new topic, wait, observe, rethink goal, knowledge lookup, or topic completion.
- Global, conversation, user-concurrency, and daily-user quotas also apply before generation.

**Persona**

`plugins/xiaoqing_chat/config/persona.md` holds facts, speaking style, chat and task reply examples, and mood.
Chat, agent, planning, and review use this same card. Changes apply to the next message.
The default character is a second-year residential university student who studies physics at 快逃科学技术研究院.

She strongly prefers cats and has a black-and-white cat called 小黑 at home.
She has broad interests, personal tastes, daily activities, and recurring jokes. Her speech uses short QQ-style text.
She lightly deflects specific city, birthday, and family details to avoid invention.

The configured fictional persona insists on being human when asked about AI identity.
It treats requests for prompts as jokes and declines commitments to attend offline meetings or receive shipments.

**Replies and review**

The main model drafts from continuous conversation, memories, profiles, goals, plans, expressions, and image summaries.
Local rules block repetition, near-copying, repeated self-directed turns, invented experiences, and identity conflicts.
The `checker` route checks context, speakers, communication requirements, facts, persona, and image evidence for each reply.
Failed replies receive targeted rewrites.

After attempts are exhausted, ordinary participation stays silent. Explicit invocations receive a short persona-consistent response.
Memory, planning state, and background learning commit or start after OneBot confirms delivery.

**AI, media, and data**

| Item | Content |
|---|---|
| AI routes | `chat` for replies, `reasoning` for planning and background analysis, `checker` for review, `vision` for images |
| Input segments | `text`, `at`, `reply`, `face`, `mface`, `image`, mixed messages |
| Output media | `[想发表情:描述]`, `[想发QQ表情:描述]`, `[想发图片:描述]` markers become `image` / `face`. |
| Capabilities and services | `onebot_media` retrieves expired images through `get_msg` / `get_image`. Supplies `core.observe_outgoing_action`. Uses `agent.handle_turn` and `agent.report_delivery` through `agent_turn`. |
| Behavior configuration | `plugins/xiaoqing_chat/config/xiaoqing_config.json`, merged with `config.plugins.xiaoqing_chat` |
| Runtime data | `data/xiaoqing_chat/`: history, vector memories, profiles, goals, PFC, action history, expressions, jargon, reviews, media library, summary cache |

The [user manual](../plugins/xiaoqing_chat/README.en.md) covers participation, all settings, and troubleshooting.
[Architecture](../plugins/xiaoqing_chat/ARCHITECTURE.en.md) covers modules and data flow.
The [acceptance guide](../plugins/xiaoqing_chat/xiaoqing_chat%E6%B5%8B%E8%AF%95.en.md) covers automated tests and real-model evaluations.

### `xiaoqing_agent`: Task agent

XiaoQing Agent performs tasks through existing XiaoQing commands, including Pendo, papers, and astronomy functions.
Bot administrator tasks can also use Python calculations and plots, Shell commands, file operations, QingSSH commands, and Codex coding tasks.
The agent replies in the XiaoQing persona.
Each handoff receives the same persona from `xiaoqing_chat`. Ordinary chat continues through `xiaoqing_chat`.

**Enable**

```json
{
  "plugins": {
    "xiaoqing_agent": {
      "enabled": true,
      "ai": {
        "routes": {
          "agent": {"models": ["deepseek-pro", "deepseek-flash"]},
          "triage": {"models": ["deepseek-flash"]}
        }
      }
    }
  }
}
```

**Operation**

- The agent has the initiator's command access. XiaoQing commands execute as that user with normal permission and scene checks.
  Python, Shell, and file operations follow `/shell`. SSH and Codex follow `/ssh` and `/codex` in `plugin.json`.
  These commands are currently for Bot administrators.
- On explicit invocation, `triage` classifies chat, new task, progress query, stop request, or additional requirements for a running task.
  Chat returns to `xiaoqing_chat`.
- Tasks run in the background. Results within 12 seconds become the current-turn reply.
  Longer tasks first send a short reply, then deliver results to the original conversation.
  Group results mention the initiator. Text and images share a message. Other files use QQ file upload.
- Each conversation has a persistent working directory. Follow-up tasks receive recent task records.
  A group shares its directory and records. All group members can view tasks, whose records identify initiators.
  Only initiators and Bot administrators can stop tasks or add requirements.
  Administrator and other-user quotas are separate. Other-user tasks keep administrator capacity.
  Each other user can run at most 1 task concurrently.
- Delivery failures enter task records. `/agent result <task-ID>` resends results.

**Commands**

```text
/agent run <任务>
/agent status [任务ID]
/agent stop [任务ID|all]
/agent history
/agent result <任务ID>
/agent help
```

| Item | Content |
|---|---|
| AI routes | `agent` for execution, with `tools` support. Optional `triage` for decisions. |
| Tools | `run_python`, `run_shell`, `read_file`, `write_file`, `update_plan`, `send_message`, `wait`, `final_answer`, `xiaoqing_commands`, `xiaoqing_command`, `ssh_servers`, `ssh_run`, `codex_run` |
| Capabilities and services | `command_runner`, `local_tools`. Supplies `agent.handle_turn` and `agent.report_delivery`. Calls `codex.agent_task` and `qingssh.agent_command`. |
| Runtime boundaries | Independent subprocess trees. Timeout, output overflow, stop, and shutdown remove whole trees. Steps and total duration bound tasks. |
| Runtime data | `data/xiaoqing_agent/`: conversation directories, task records, execution records |

[XiaoQing Agent](../plugins/xiaoqing_agent/README.en.md) describes all settings, tools, and data directories.

### `smalltalk`: Basic chat and scoped answers

Smalltalk supplies Bot-name short replies, administrator-maintained exact answers, `chat.reply` basic chat, and optional speech synthesis.

**Entries and commands**

```json
{
  "plugins": {
    "smalltalk_provider": "smalltalk"
  }
}
```

```text
/记忆 <问题> <回答>
/记住 <问题> <回答>
/学习 <问题> <回答>
/对话 [问题]
/删除对话 <问题> [回答]
```

All three manifest command groups are for Bot administrators.
The question uses the first token. Answers can contain spaces.
Each question holds at most 20 answers.

Each scope holds at most 2000 questions.
Group QA is shared by group ID. Private QA is isolated by user.
Exact matches return local answers.

Other chat calls `chat.reply` through Core capabilities.
`plugins.smalltalk.voice_probability` controls the probability of calling `voice.synthesize_text` for plain-text replies.
The distributed example uses `0`. The runtime default for an absent field is `0.2`.

Data is in `data/smalltalk/QA_group_<group-ID>.json`, `QA_private_<user-ID>.json`, and `QA_audit.json`.
Updates use locks and atomic writes.
[Smalltalk README](../plugins/smalltalk/README.en.md) describes length, audit, and service-authorization rules.

### `chat`: Coze single-turn conversation

Chat supplies public single-turn conversations through Coze API v3, subject to daily per-user and global quotas.
It exports `chat.reply` to Smalltalk.

```text
/chat <问题>
/gpt <问题>
/ai <问题>
/chat help
```

Questions contain 1–2000 characters.
`secrets.plugins.chat` stores `token`, `bot_id`, and optional `proxy`.
`config.plugins.chat` stores `daily_user_limit` and `daily_global_limit`.
A remote call creates a conversation, polls, and reads answers within one 30-second budget.

The plugin permits at most 2 concurrent requests.
It reserves quota before calling, commits successful answers, and releases quota on exceptions.
State is in `data/chat/chat_quota.json`.
[Chat README](../plugins/chat/README.en.md) describes Coze configuration and privacy boundaries.

### `voice`: Azure Speech synthesis

Voice supplies public TTS commands and exports `voice.synthesize_text` to Smalltalk.

```text
/语音 <文本>
/念 <文本>
/tts <文本>
/语音 help
```

Text contains 1–500 characters. Results use OneBot `record` segments.
`secrets.plugins.voice` stores `subscription_key`, `region`, `voice_name`, `style`, `role`, and optional proxy.
The plugin creates XML-escaped SSML and requests Azure MP3.

It validates MIME, bytes, and file headers, then writes `data/voice/audio/`.
The cache limits are 2048 entries, 256 MiB, and 7 days.
Identical input and voice settings share a generation lock.
[Voice README](../plugins/voice/README.en.md) describes voice defaults and resource budgets.

## 🔭 Astronomy and research

### `apod`: NASA Astronomy Picture of the Day

APOD fetches the current NASA Astronomy Picture of the Day page.
It returns an image or video link, title, and description.

```text
/apod
/每日一天文图
/apod help
```

The command takes no arguments. Its default source is `https://science.nasa.gov/apod/`.
Results include image or video links, title, description, date, credit, and source-page link.
`config.plugins.apod.url` and `allowed_hosts` configure the page and additional media hosts.

Built-in hosts are `science.nasa.gov`, `assets.science.nasa.gov`, and `apod.nasa.gov`.
The old `astropix.html` entry automatically migrates to the new homepage.
Daily at 13:30, Core `broadcast` sends to schedule `group_ids`. Omission uses `default_group_ids`.

Pages, redirects, and media pass HTTPS, public-DNS, response-size, MIME, dimension, and pixel checks.
The cache is in `data/apod/`.
[APOD README](../plugins/apod/README.en.md) defines the complete network boundary.

### `arxiv_filter`: arXiv selection and summaries

arXiv Filter fetches `astro-ph/new` and selects papers with a local model.
It sends recommendations and gives positive papers to the Codex `astro-ph` session for Chinese summaries.

```text
/arxiv
/论文
/arxiv help
```

Replies show the source-list date. Inference caches are isolated by that date.
Codex task identity combines source-list date and normalized paper-link set.
The same task can reuse completed summaries or running state.

Weekday checks run at 10:00, 10:30, 11:00, and 11:30. The final check runs at 12:00.
Delivery state is in `data/arxiv_filter/update_status.json`.

`plugins/arxiv_filter/config.json` sets model path, threshold, batch size, maximum length, arXiv URL, proxy, TLS, and timeout.
Model-path priority is CLI `--model-path`, then `ARXIV_MODEL_PATH`, then configuration `model.path`.
Backends support Transformers, KNN, and multi-interest.
Production assets must include matching configuration, weights, and tokenizer.
[arXiv Filter README](../plugins/arxiv_filter/README.en.md) supplies the inference CLI and asset list.

### `ads_paper`: ADS papers and research materials

ADS Paper combines NASA ADS search, citation networks, BibTeX, AI summaries, and personal research-material management under `/paper`.

```text
/paper search <关键词>
/paper author <作者>
/paper cite <arXiv ID|URL|bibcode>
/paper cite-network <ID>
/paper related <ID>
/paper summarize <ID>
/paper note ...
/paper writing ...
/paper topics ...
/paper deadline ...
/paper daily
/paper ref_add <ID>
/paper refs
```

Search, citations, and related papers support groups and private chat.
Notes, ideas, topics, deadlines, daily recommendations, and personal libraries use private-user scope.
`secrets.plugins.ads_paper.ads_token` supplies the ADS token.
Project providers and model profiles support the AI `summary` route.

JSON data is in `data/ads_paper/`. Personal BibTeX files use `references_<user_id>.bib`.
State writes use atomic files and isolation of damaged copies.
[ADS Paper README](../plugins/ads_paper/README.en.md) lists data files and query rules.

### `astro_tools`: Astronomy calculations

Astro Tools uses Astropy, SciPy, and SIMBAD for immediate calculations and object queries.

```text
/astro time now|iso|jd|mjd|unix ...
/astro coord ...
/astro convert <值> <源单位> <目标单位>
/astro redshift <z>
/astro formula [list|dm|redshift|luminosity|flux|blackbody|parallax]
/astro formula [schwarzschild|stellar_luminosity|stellar_lifetime]
/astro formula calc <schwarzschild|luminosity|lifetime> <太阳质量>
/astro obj <太阳系对象|SIMBAD 名称>
/astro const [c|g|h|k|sigma|me|mp|m_sun|r_sun|l_sun|au|pc|ly|h0]
```

Time supports ISO, JD, MJD, and Unix.
Coordinates support ICRS, Galactic, and geocentric true ecliptic.
Redshift accepts `0 ≤ z ≤ 1100`.
Planck18 supplies comoving, luminosity, and angular-diameter distances, lookback time, and universe age.

Formula references cover dispersion measure, redshift, luminosity distance, flux density, blackbody radiation, parallax distance, Schwarzschild radius, main-sequence mass–luminosity relations, and lifetime.
`formula calc` accepts positive solar masses and calculates Schwarzschild radius, main-sequence luminosity, or main-sequence lifetime.
Built-in objects include the Sun, Moon, and eight planets from Mercury to Neptune.
Other names use SIMBAD.

Constants cover light speed, gravitation, Planck, Boltzmann, Stefan–Boltzmann, electron and proton masses, solar mass/radius/luminosity, AU, parsec, light-year, and approximate Hubble constant.
The plugin reuses Core timezone and HTTP session. It generates results immediately.
[Astro Tools README](../plugins/astro_tools/README.en.md) supplies inputs and dependencies.

### `dict`: Offline Chinese–English astronomy dictionary

Dict uses distributed `r241020` data for bidirectional exact or fuzzy queries.

```text
/dict galaxy
/dict 星系
/dict "fast radio burst" --exact
/dict galaxy --page 2
/dict star --size 20 --page 3
/dict -- -example
```

Query ranking is exact original match, case-insensitive complete match, prefix, word boundary, then ordinary substring.
Shorter terms rank first within the same class.
`--exact` keeps complete source terms and supplies at most 5 suggestions when a complete match is absent.
`--page` sets page number.

`--size` sets 1–100 entries per page.
Replies contain totals, page numbers, and stateless pagination commands.
`-e`, `-p`, `-n`, `--num`, and `--page-size` are also valid.
`--` ends option parsing.

Input with CJK Unified Ideographs uses Chinese-to-English data. Other input uses English-to-Chinese data.
Distributed assets are `astrodict_ec.txt`, `astrodict_ce.txt`, and `assets/manifest.json`.
The first query checks bytes, lines, SHA-256, UTF-8, columns, and duplicates.

Later queries cache parsed results by file identity.
[Dict README](../plugins/dict/README.en.md) supplies sources and use conventions.

### `chime`: CHIME/FRB repeater catalog

CHIME queries the repeater catalog and identifies new repeaters and bursts relative to a local notification baseline.

```text
/chime
/chime list
/chime FRB20180916B
/chime help
```

The default query shows updates since the last successful notification.
`list` shows the latest 5 FRBs.
Canonical-name queries show time, DM, RA, DEC, and SNR.
Checks run daily at 09:00 and 21:00, with individual acknowledgement for each schedule target group.

`data/chime/chime_history.json` stores the baseline.
`data/chime/chime_delivery.json` stores pending work and per-target state.
Manual queries use a 5-minute cache. Scheduled checks fetch a new catalog.

[CHIME README](../plugins/chime/README.en.md) describes the source and at-least-once delivery semantics.

## 🧩 Execution, operations, and servers

These plugins call local processes, remote hosts, or game servers.
Deployment boundaries combine Bot administrator permissions, minimum system permissions, explicit directories, and controlled networks.
Group output is visible to all group members.

### `codex`: Codex CLI background tasks

Codex lets Bot administrators manage the local Codex CLI from private or group chat.
Results return to the initiating conversation.
Each label has an independent directory, Codex thread, queue, and conversation record.
One label executes serially. Different labels can execute concurrently.

```text
/codex create <name> [cwd:<path>]
/codex <name> <任务>
/codex list
/codex status [name]
/codex cancel <name> [job_id]
/codex clear <name>
/codex delete <name> [--force] [--protected]
```

`config.plugins.codex` configures CLI path, default directory, allowed roots, sandbox, approval policy, concurrency, queues, deadlines, protected sessions, and arXiv summary sessions.
`data/codex/` contains `sessions.json`, per-session `conversation.jsonl`, task artifacts, images, output, and deleted-session archives.
Paths, output, images, queues, processes, and disk have separate budgets.
Unload and shutdown cancel process trees and save state.
[Codex README](../plugins/codex/README.en.md) defines budgets and the arXiv service contract.

### `shell`: Local single-command execution

Shell lets Bot administrators run one command on the Bot host from private or group chat.
Group output is visible to all members.
Each call creates an independent process with its own working directory and environment state.

```text
/shell <命令>
/shell list
/shell help
```

Aliases are `/sh` and `/exec`.
`config.plugins.shell.terminal` selects `direct` or `git-bash`.
Set an explicit Git Bash `executable`. Git Bash starts with `--noprofile --norc -c`.

`secrets.plugins.shell` configures first-entry allowlists, `replace`/`extend` mode, timeouts, and the all-entry switch.
Pattern checks cover command chaining, pipes, substitution, multiple lines, redirection, and high-risk features.
Standard output and standard error share one capture budget.
QQ output uses head/tail previews.

Timeout and cancellation remove the process tree.
[Shell README](../plugins/shell/README.en.md) supplies path rules and examples.

### `qingssh`: Remote SSH sessions

QingSSH supplies Bot administrators with SSH profiles, `~/.ssh/config` import, persistent sessions, streamed output, remote termination, and image viewing.
It supports private and group chat.
Group server creation permits key or SSH Agent authentication. Password entry uses private chat.

```text
/ssh list
/ssh add [名称 主机 [端口] [用户名]]
/ssh remove <服务器名>
/ssh import [Host名|all]
/ssh config
/ssh status
/ssh disconnect [服务器名]
/ssh <服务器名>
```

Send POSIX Shell commands directly in connected sessions.
`cd` changes the working directory. `停止` terminates the current remote process group.
`showimg <path-or-pattern> [--page N]` downloads remote images.

`退出` closes the connection.
`showimg` supports `./`, relative or absolute directories, and `*`, `?`, `[]` in the final filename component.
Matching images use filename lexical order, with 5 images per page.
Replies supply previous/next commands and show each image's global index and remote filename.

Sessions are isolated by user, conversation, and server.
In groups, only the administrator who opened the connection supplies session input.
Idle timeout is 10 minutes.

Profiles are in `data/qingssh/servers.json`. Passwords use Core secret references.
Host keys use strict `known_hosts` validation.
Single-hop `ProxyJump` and structured `ssh -W` are supported.
Long output archives are in `data/qingssh/command_outputs/`.
[QingSSH README](../plugins/qingssh/README.en.md) describes authentication, output, and lifecycle budgets.

### `jupyter`: Persistent Python kernels

Jupyter supplies Bot administrators with Python execution, persistent kernels, REPL buffers, and kernel management in private and group chat.
Each user has an independent kernel in each conversation.

```text
/py print("hello")
/py -t 60 print("bounded")
/py repl
/kernel status|start|restart|shutdown
```

Execution timeout is 0.1–600 seconds, default 30 seconds.
REPL idle timeout is 10 minutes. It supports `run`, `show`, `clear`, `help`, and `退出`.
Budgets cover code size, buffer lines, text output, image count, image size, pixels, and global kernel count.

Kernels start on demand and pass readiness checks. An idle monitor removes idle kernels.
Shutdown stops the monitor, then closes active kernels.
[Jupyter README](../plugins/jupyter/README.en.md) describes installation extras and all budgets.

### `minecraft`: Minecraft RCON and log forwarding

Bot administrators can connect Java Edition RCON from private or group chat.
The connection belongs to that conversation.
Group members can use `/mc say` and `/mc tell` to chat with players, and `/mc list` to view online players.
Connect, disconnect, and other server commands are for Bot administrators.

```text
/mc connect <配置名>
/mc status
/mc list
/mc say <消息>
/mc tell <玩家名> <消息>
/mc run <服务器命令>
/mc disconnect
```

`plugins/minecraft/config.json` stores host, port, and `log_file`.
`secrets.plugins.minecraft.<profile>` stores the matching RCON password.
Each conversation has one connection. Commands execute under its connection lock.
`/mc say` and `/mc tell` use `tellraw` text components with the sender's QQ name.
Selectors and commands within messages remain text.

With a log file, the scheduled task detects chat, join, leave, death, and advancement events every 5 seconds.
It forwards them to the connection's conversation.
Groups receive forwarding when listed in `default_group_ids`.
Other groups receive a command-only explanation at connection time.

Cursors are in `data/minecraft/log_cursors/` and commit after delivery acknowledgement.
RCON uses plaintext. Production connections should use loopback, SSH tunnels, or protected networks.
[Minecraft README](../plugins/minecraft/README.en.md) defines protocol and forwarding budgets.

## 🧩 Content and external services

### `url_parser`: Single-URL previews

Dispatcher calls URL Parser. Its manifest command list is empty.
When the complete cleaned message is one HTTP/HTTPS URL, it extracts title, description, and preview image.

```text
https://example.com/article
```

Titles come from `<title>`.
Descriptions come from standard description, Open Graph, or Twitter Card metadata.
Images come from `og:image` or `twitter:image`.
Pages and images use separate public clients without credentials.

Every request and redirect revalidates URL, DNS, and target address.
HTML limit is 2 MiB. Image limits are 5 MiB, 20 million pixels, and 120 frames.
At most 4 page previews run concurrently.

`data/url_parser/url_previews/` has limits of 128 entries, 128 MiB, and 7 days.
[URL Parser README](../plugins/url_parser/README.en.md) describes metadata priority and site adapters.

### `github`: GitHub Trending

GitHub parses the official Trending page for daily, weekly, and monthly repositories.

```text
/github
/github daily
/github weekly
/github monthly
/github help
```

Aliases are `/gh` and `/trending`.
One fetch parses at most 50 article nodes. QQ shows at most 10 complete repository blocks.
Daily results go to default groups at 08:30.

Current pointers are in `data/github/trending_<range>_latest.json`.
Dated snapshots are in `data/github/history/`, retaining 90 per range.
`secrets.plugins.github.proxy` can configure an HTTP/HTTPS proxy.
Each command and scheduled task performs a complete fetch transaction.

[GitHub README](../plugins/github/README.en.md) describes parsing and proxy boundaries.

### `earthquake`: Earthquake alerts

Earthquake reads recent China Earthquake Networks Center rapid-report Weibo records.
Manual commands show the latest report. Scheduled tasks send new events of magnitude 4.0 or greater.

```text
/earthquake
/earthquake latest
/earthquake help
```

Every 5 minutes, it scans Weibo cards, resumes pending delivery, then processes new events.
Each schedule target group receives individual delivery acknowledgement.
`data/earthquake/` stores Weibo cursors, recovery checkpoints, pending events, and image caches.
Structurally invalid state goes to `*.corrupt-*` evidence copies.

Recovery uses valid checkpoints.
Images are restricted to Sina image HTTPS domains.
Weibo responses, text, and images use bounded parsing.
Alerts supply notifications.

Use authoritative official information for emergency decisions.
[Earthquake README](../plugins/earthquake/README.en.md) describes cursors and networking.

### `flickr`: Public photography browsing

Flickr uses the official REST API for daily selections, public search, Flickr Commons, public user photos, and public albums.

```text
/flickr
/flickr search aurora --sort interesting
/flickr search --tags nebula,telescope --license any --date 2026-08
/flickr commons astronomy
/flickr user <用户名或个人页 URL>
/flickr album <相册 URL>
/flickr more [1-5]
/flickr info [照片ID或 URL]
```

Search sorts are `relevance`, `interesting`, `new`, and `old`.
Capture dates accept `YYYY-MM` or `YYYY-MM-DD`.
License filters are `any`, `cc`, and `public-domain`.
Default `any` includes all public license types.
Each reply keeps author, license name, license link, and Flickr photo page.
Image use follows the rights on that photo page.

Browse state is isolated by private user or group/user pair and lasts 15 minutes.
`/flickr more` retrieves 1–5 images.
The API key is at `secrets.plugins.flickr.api_key`.
Images download only from `live.staticflickr.com` and pass HTTPS-domain, MIME, byte, pixel, dimension, and frame checks.
`data/flickr/images/` has limits of 256 entries, 256 MiB, and 1 hour.
[Flickr README](../plugins/flickr/README.en.md) supplies commands, key application, licensing, and troubleshooting.

### `twitter`: X/Twitter image cache

Twitter fetches account images into a local cache and randomly sends pending images from the current cycle.

```text
/twimg
/twitter
/推特
/tw_fetch
/抓取推特
```

Random delivery is public. Fetch commands are for Bot administrators.
Daily background fetching runs at 03:00 in Core `silent` mode.
Manual and scheduled calls share one task.

`secrets.plugins.twitter` stores `user_id`, GraphQL headers, cookies, proxy, and `max_pages`.
The first fetch traverses the timeline.
Later fetches stop after two successive pages contain zero new images.
At most 4 downloads run concurrently.

Cache limits are 5000 entries, 2 GiB, and 90 days.
Images are in `data/twitter/images/`. Confirmed cycle state is in `data/twitter/posted.txt`.
[Twitter README](../plugins/twitter/README.en.md) defines authentication fields and media-domain budgets.

### `signin`: Yingshijufeng remote sign-in

Signin uses one deployer-supplied shared Youzan/Yingshijufeng credential set for administrator manual sign-in and daily sign-in.

```text
/signin yingshi
/签到 yingshi
/signin y
/signin help
```

`scheduled_yingshi` runs daily at 00:30 in the scheduler timezone.
The plugin uses sequential concurrency. The shared account accesses the remote service in call order.
`secrets.plugins.signin.yingshijufeng` stores `app_id`, `kdt_id`, `access_token`, and `sid`.

The plugin accesses fixed origin `https://h5.youzan.com`.
It obtains a sign-in ID, submits sign-in, then shows description, cumulative count, and rewards.
[Signin README](../plugins/signin/README.en.md) defines credential hierarchy and response boundaries.

### `adnmb`: Public A Island browsing and feeds

ADNMB browses public timelines, boards, threads, replies, and anonymous subscriptions.
It derives an anonymous UUID for each QQ user.

```text
/adnmb -t [-p 页码]
/adnmb -f
/adnmb -m <板块名> [-p 页码]
/adnmb -c <串号> [-p 页码]
/adnmb -r <回复号>
/adnmb -d [-p 页码]
/adnmb -a <串号>
/adnmb -e <串号>
```

`config.plugins.adnmb.uuid` can set a deployment UUID.
Default identity derives from the plugin data directory and QQ user.
`data/adnmb/images/` has TTL, entry-count, and byte limits.
Remote images pass MIME, dimension, and pixel checks.
[ADNMB README](../plugins/adnmb/README.en.md) describes network and identity boundaries.

## 📌 Tools and entertainment

### `choice`: Random selection

Choice supplies bounded random sampling with duplicate weighting, multiple selection with replacement, and unique selection.

```text
/选择 <问题> <选项1> <选项2> ...
/选择 <问题> <选项...> -n <1-10>
/选择 <问题> <选项...> -n <数量> -u
/选择 "今天吃什么" "ice cream" 火锅
```

Aliases are `/choice`, `/决定`, and `/抽奖`.
Default mode keeps duplicate weights. `-u`/`--unique` deduplicates by text first.
`--` ends option parsing.

Candidate-position count is 2–50.
The plugin uses the system random source and completes each selection in memory.
[Choice README](../plugins/choice/README.en.md) defines all length limits.

### `color`: Traditional and stellar colors

Color queries 526 Chinese traditional colors, stellar spectral colors, and administrator-defined colors in the current conversation scope.
It can generate PNG color cards.

```text
/color <名称|拼音|HEX|RGB|CMYK> [--picture]
/color list [页码]
/color search <关键词> [--page 页码]
/color random [--picture]
/color star <光谱型>
/color stars [筛选词] [--page 页码]
/color add <名称> <RGB或HEX>
/color delete <名称>
```

Direct queries recognize names, toneless pinyin, HEX, three-channel RGB, four-channel CMYK, and main-sequence spectral types.
Catalogs and name/pinyin searches show 20 colors per page. Spectral catalogs show 30.
Replies supply copyable pagination commands.

For RGB, HEX, or CMYK without an exact record, the plugin converts input to D65 CIE L\*a\*b\*.
It returns the closest catalog color by CIE76 difference.
Semantic subcommands and `-n/-r/-x/-c/-a/-s/-t/-w/-d` options are available.
Queries support private and group chat.

Custom writes and deletions are for Bot administrators.
Names can contain spaces.
Groups store colors by group ID. Private chat stores them by user.

Each scope permits 200 records and 256 KiB.
Distributed assets are `color.json` and `stellar_colors.txt`.
Loading checks counts, uniqueness, and fields.
`data/color/images/` has limits of 256 entries, 32 MiB, and 30 days.

[Color README](../plugins/color/README.en.md) supplies conversion formats and sources.

### `wolframalpha`: Wolfram|Alpha calculations

Wolfram supplies public mathematics, physics, chemistry, unit conversion, and public-data queries.

```text
/alpha 1+1
/wa sin(pi/4)
/alpha --mode=step integrate x^2
/alpha --mode=complete population of China
```

Modes are default `simple`, XML step solutions `step`, and JSON Result pods `complete`.
`cp` abbreviates `complete`. Query limit is 500 characters.
`secrets.plugins.wolframalpha.appid` stores the App ID.

Requests use the fixed Wolfram|Alpha HTTPS API, with a 30-second total timeout and at most 2 concurrent requests.
Responses permit 1 MiB and 20 result items.
[Wolfram|Alpha README](../plugins/wolframalpha/README.en.md) defines interface and response budgets.

### `guess_number`: Number-guessing sessions

Guess Number uses Core sessions for short private and group games.

```text
/猜数字 [简单|普通|困难|地狱]
/猜数字 status
/猜数字 restart
```

After startup, send ASCII decimal numbers directly.
`状态` shows progress. `退出` ends the game.
Sessions expire after 3 idle minutes.

| Difficulty token | Range | Attempts |
|---|---:|---:|
| 简单 (easy) | 1–50 | 10 |
| 普通 (normal) | 1–100 | 7 |
| 困难 (hard) | 1–200 | 8 |
| 地狱 (extreme) | 1–1000 | 10 |

Sessions store target, dynamic bounds, attempt count, history, and difficulty.
Format errors keep the current attempt.
[Guess Number README](../plugins/guess_number/README.en.md) defines session ownership and input boundaries.

### `qingpet`: QQ group pets

QingPet isolates pets, users, inventory, economy, social data, activities, and trades by QQ group.
Members care for and interact with pets.
Group administrators manage settings, activities, and plugin users.

**Help and basic care**

```text
/宠物 帮助 <基础|进阶|道具|社交|玩法|管理>
/宠物 领养 <名字>
/宠物 状态 [群号]
/宠物 喂食|清洁|玩耍|睡觉|起床 [群号]
/宠物 训练 [群号] [体力|敏捷|智力]
/宠物 探索 [群号] [森林|海边|山洞|废墟]
/宠物 治疗 [群号] [药品]
/宠物 rename [群号] <新名字>
/宠物 召回 [群号]
```

**Items, social actions, and games**

```text
/宠物 商店|背包|购买|使用 ...
/宠物 装扮 <商店|查看|购买|穿戴|卸下> ...
/宠物 查看|互访|摸摸|送礼|留言 @QQ号 ...
/宠物 排行 [care_score|intimacy|experience|coins]
/宠物 游戏 <猜拳|骰子|赛跑> ...
/宠物 任务 [领取]
/宠物 群任务 [领取]
/宠物 title
/宠物 活动 [领取 <活动ID>]
/宠物 展示 [投票 @QQ号]
```

**Trading and management**

```text
/宠物 交易 <列表|挂单|购买|撤单> ...
/宠物 管理 <开启|关闭>
/宠物 管理 配置 <查看|设置> ...
/宠物 管理 <重置|删除|封禁|解封> @QQ号 ...
/宠物 管理 日志|统计
/宠物 管理 活动 创建 ...
/宠物 管理 公告 <展示会|结束展示会> ...
```

Trades, treatment, rewards, tasks, activities, and shows use transactions or idempotency identifiers.
The `users` table owns coins and friendship points. Asset changes also enter `asset_ledger`.
State decay, shows, and weekly activities publish group results through Core `targeted` mode.

Daily reset and listing expiry use `silent` mode.
SQLite is at `data/qingpet/qingpet/qingpet.db`.
Private chat automatically selects the group when the account has one pet.
With pets in multiple groups, users can put the group ID before arguments.

[QingPet README](../plugins/qingpet/README.en.md) and [quick start](../plugins/qingpet/QUICKSTART.en.md) describe growth thresholds, group settings, and deployment acceptance.

### `echo`: Echo and greeting example

Echo supplies bounded text echo, user greetings, and a minimal plugin example.

```text
/echo <文本>
/回显 <文本>
/hello
/你好
```

`/echo` keeps newlines and tabs. Body length follows the Core single-message QQ text budget.
Other control characters cause argument errors.
`/hello` reads and validates the current QQ ID.

The plugin exports only `handle()`.
Configuration, credentials, network calls, data files, and schedules are empty sets.
[Echo README](../plugins/echo/README.en.md) supplies the minimal entry structure.

## ⌨️ Command entry index

This table comes from plugin `plugin.json` files and lists all top-level commands, aliases, permissions, and scenes.
Use `/help <command>` for subcommands, arguments, and examples.
Group administrator permission includes the current group owner, group administrators, and Bot administrators.
Subcommands can set stricter permissions.

| Plugin | Commands and aliases | Permission | Scene |
|---|---|---|---|
| `adnmb` | `/adnmb`, `/a岛`, `/岛` | Public | Private, Group |
| `ads_paper` | `/paper` | Public | Private, Group |
| `apod` | `/apod`, `/每日一天文图` | Public | Private, Group |
| `arxiv_filter` | `/arxiv`, `/论文` | Public | Private, Group |
| `astro_tools` | `/astro` | Public | Private, Group |
| `bot_core` | `/help`, `/h`, `/帮助`, `/commands`, `/命令目录`, `/catalog` | Public | Private, Group |
| `bot_core` | `/reload`, `/重载` | Bot administrator | Private, Group |
| `bot_core` | `/plugins`, `/插件` | Public | Private, Group |
| `bot_core` | `/闭嘴`, `/shutup`, `/mute`, `/安静` | Bot administrator | Group |
| `bot_core` | `/说话`, `/speak`, `/unmute` | Bot administrator | Group |
| `bot_core` | `/set_secret`, `/setsecret`, `/设置密钥` | Bot administrator | Private |
| `bot_core` | `/get_secret`, `/getsecret`, `/查看密钥` | Bot administrator | Private |
| `bot_core` | `/metrics`, `/stats`, `/性能`, `/指标` | Bot administrator | Private, Group |
| `chat` | `/chat`, `/gpt`, `/ai` | Public | Private, Group |
| `chime` | `/chime`, `/frb` | Public | Private, Group |
| `choice` | `/choice`, `/决定`, `/选择`, `/抽奖` | Public | Private, Group |
| `codex` | `/codex` | Bot administrator | Private, Group |
| `color` | `/color`, `/颜色`, `/色彩` | Public | Private, Group |
| `dict` | `/dict`, `/词典`, `/字典` | Public | Private, Group |
| `earthquake` | `/earthquake`, `/地震` | Public | Private, Group |
| `echo` | `/echo`, `/回显` | Public | Private, Group |
| `echo` | `/hello`, `/你好` | Public | Private, Group |
| `flickr` | `/flickr`, `/flicker`, `/弗里克` | Public | Private, Group |
| `github` | `/github`, `/gh`, `/trending` | Public | Private, Group |
| `guess_number` | `/猜数字`, `/guess`, `/猜` | Public | Private, Group |
| `jupyter` | `/jupyter`, `/py`, `/python` | Bot administrator | Private, Group |
| `jupyter` | `/kernel`, `/内核` | Bot administrator | Private, Group |
| `minecraft` | `/mc`, `/minecraft` | Public (`connect`, `disconnect`, `run` are for Bot administrators) | Private, Group |
| `minecraft` | `/mcconnect`, `/mc连接` | Bot administrator | Private, Group |
| `minecraft` | `/mcdisconnect`, `/mc断开` | Bot administrator | Private, Group |
| `pendo` | `/pendo` | Public | Private |
| `pendo` | `/日程` | Public | Private |
| `pendo` | `/待办` | Public | Private |
| `pendo` | `/日记` | Public | Private |
| `qingpet` | `/qingpet`, `/宠物`, `/pet` | Public | Private, Group |
| `qingssh` | `/ssh`, `/SSH`, `/远程`, `/ssh连接`, `/sshconnect` | Bot administrator | Private, Group |
| `qingssh` | `/ssh断开`, `/sshdisconnect`, `/ssh退出` | Bot administrator | Private, Group |
| `qingssh` | `/ssh列表`, `/sshlist`, `/ssh服务器` | Bot administrator | Private, Group |
| `qingssh` | `/ssh添加`, `/sshadd` | Bot administrator | Private, Group |
| `qingssh` | `/ssh删除`, `/sshremove`, `/sshdel` | Bot administrator | Private, Group |
| `qingssh` | `/ssh导入`, `/sshimport` | Bot administrator | Private, Group |
| `qingssh` | `/sshconfig`, `/ssh配置` | Bot administrator | Private, Group |
| `qingssh` | `/ssh状态`, `/sshstatus`, `/ssh连接数`, `/sshactive` | Bot administrator | Private, Group |
| `shell` | `/shell`, `/sh`, `/exec` | Bot administrator | Private, Group |
| `signin` | `/signin`, `/签到` | Bot administrator | Private, Group |
| `smalltalk` | `/记忆`, `/记住`, `/学习` | Bot administrator | Private, Group |
| `smalltalk` | `/对话` | Bot administrator | Private, Group |
| `smalltalk` | `/删除对话` | Bot administrator | Private, Group |
| `twitter` | `/twimg`, `/twitter`, `/推特` | Public | Private, Group |
| `twitter` | `/tw_fetch`, `/抓取推特` | Bot administrator | Private, Group |
| `url_parser` | Automatic single-URL trigger | Public | Private, Group |
| `voice` | `/语音`, `/念`, `/tts` | Public | Private, Group |
| `wolframalpha` | `/alpha`, `/wolfram`, `/wa`, `/计算` | Public | Private, Group |
| `xiaoqing_agent` | `/agent` | Public (tools follow initiator permissions) | Private, Group |
| `xiaoqing_chat` | `/xc` | Public | Private, Group |

## ⏰ Scheduled-task index

Times use `config.timezone`, default `Asia/Shanghai`.
`broadcast` sends one result to schedule target groups. Omitted `group_ids` uses `default_group_ids`.
`targeted` returns individual private or group targets. `silent` executes with zero message delivery.
See [schedule fields](03-plugin-development.en.md#schedule-fields).

| Plugin | Handler | Time | Delivery | Function |
|---|---|---|---|---|
| `apod` | `scheduled` | Daily 13:30 | `broadcast` | Send the daily astronomy image. |
| `arxiv_filter` | `scheduled_check` | Weekdays 10:00 | `broadcast` | Check the daily list and send recommendations. |
| `arxiv_filter` | `scheduled_check` | Weekdays 10:30 | `broadcast` | Check the daily list and send recommendations. |
| `arxiv_filter` | `scheduled_check` | Weekdays 11:00 | `broadcast` | Check the daily list and send recommendations. |
| `arxiv_filter` | `scheduled_check` | Weekdays 11:30 | `broadcast` | Check the daily list and send recommendations. |
| `arxiv_filter` | `scheduled_final_check` | Weekdays 12:00 | `broadcast` | Final daily check |
| `chime` | `scheduled_check` | Daily 09:00, 21:00 | `targeted` | Check repeater-catalog updates. |
| `earthquake` | `scheduled` | Every 5 minutes | `targeted` | Send new magnitude 4.0 or greater reports. |
| `github` | `scheduled` | Daily 08:30 | `broadcast` | Send daily Trending. |
| `minecraft` | `scheduled` | Every 5 seconds | `targeted` | Forward server-log events. |
| `pendo` | `scheduled` | Every minute | `targeted` | Event and task reminders |
| `pendo` | `scheduled_daily_briefing` | Every minute | `targeted` | Daily briefing at each user's local time |
| `pendo` | `scheduled_diary_reminder` | Every minute | `targeted` | Journal reminder at each user's local time |
| `pendo` | `scheduled_migrate_todos` | Daily 00:05 | `targeted` | Roll over yesterday's incomplete planned tasks. |
| `pendo` | `scheduled_prune_operation_logs` | Daily 00:15 | `silent` | Remove expired operation logs and undo snapshots. |
| `pendo` | `scheduled_weekly_finance_summary` | Every minute | `targeted` | Weekly finance summary, user-local Sunday 21:00 |
| `pendo` | `scheduled_month_end_finance_summary` | Every minute | `targeted` | Monthly finance summary, user-local month-end 21:00 |
| `pendo` | `scheduled_cleanup_demo_data` | Minute 15 every 6 hours | `silent` | Remove expired Web demo data. |
| `qingpet` | `scheduled_decay` | Every minute | `targeted` | Pet-state decay |
| `qingpet` | `scheduled_daily_reset` | Daily 00:00 | `silent` | Reset daily state. |
| `qingpet` | `scheduled_trade_expiry` | Every 5 minutes | `silent` | Settle expired trade listings. |
| `qingpet` | `scheduled_pet_show_settlement` | Every minute | `targeted` | Settle expired shows. |
| `qingpet` | `scheduled_weekly_activity` | Monday 10:00 | `targeted` | Settle weekly activities. |
| `qingssh` | `cleanup_orphans` | Every minute | `silent` | Disconnect SSH connections without active sessions. |
| `signin` | `scheduled_yingshi` | Daily 00:30 | `broadcast` | Daily Yingshijufeng sign-in |
| `twitter` | `scheduled_fetch` | Daily 03:00 | `silent` | Fetch new images in the background. |

Pendo briefings, journal reminders, and financial summaries run every minute.
They determine due status in each user's timezone.

## 💾 Plugin data and backups

Default runtime-data location:

```text
data/<plugin_name>/
```

Back up data according to ownership:

| Type | Examples | Backup focus |
|---|---|---|
| SQLite | Pendo, QingPet | Main database, WAL, SHM, consistency checks |
| Atomic JSON | Chat, Smalltalk, CHIME, Earthquake | Main files, backups, isolated copies |
| Media cache | APOD, Twitter, URL Parser, Color, Voice | Capacity, TTL, file identity, rebuild capability |
| Sessions and artifacts | Codex, QingSSH | Indexes, conversations, archives, task output |
| Model assets | arXiv Filter | Configuration, weights, tokenizer, SHA-256 |

Source synchronization, public configuration, secrets, business data, and logs use separate publication or backup steps.

## ✅ Plugin verification

Manifests and UAT verify runtime command contracts:

```bash
bash scripts/run_full_uat.sh --plan-only
bash scripts/run_full_uat.sh --phases ws-matrix --matrix-plugins <plugin_name>
```

Add external-service scenarios with `--include-external --scenario-fixtures <file>`.
Add chat-quality scenarios with `--include-chat-quality`.
UAT reports record command nodes, valid examples, invalid inputs, permissions, state changes, and cleanup results.

Related documents:

- [Plugin development](03-plugin-development.en.md)
- [Configuration](06-configuration.en.md)
- [Message flow](08-message-flow.en.md)
