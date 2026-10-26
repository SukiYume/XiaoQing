# XiaoQingBot changelog

[中文](CHANGELOG.md)

This file records version changes, affected modules, and historical validation results in reverse date order.
Dates use Beijing time and `YYYY-MM-DD` format.
Validation results describe the recorded release work. They are separate from checks for this documentation revision.

- `下一版本` identifies merged or active development that is still unreleased.
- `X.Y.Z — date` identifies a release. The version agrees with `pyproject.toml` for that release.
- Date-only entries record changes before 4.1.0 and between versions.

## 4.6.0 — 2026-10-26

- Add an administrator-private Xiaomi Home and XiaoAi plugin with QR login, token renewal, capability discovery and natural-language queries and controls.
- Support sensor properties, Open-Meteo weather, configurable feeding portions and infrared lights and air conditioners. Selected infrared features use silent speaker forwarding and report unconfirmed physical effects.
- Add concise help, complete, grouped device and feature lists, and structured calls for the agent. Validate operations against device specifications, persist duplicate guards before submission and read available properties after writes.
- Add the `xiaoqing[xiaoai]` installation extra, model route example and bilingual documentation covering model-service data flow and host-side restart requirements for configuration changes.
- Add an authorized image-reading tool using the vision route, multi-keyword command search and pagination. Progress delivery accounts for the message and every attachment.
- Clarify agent rules for reminder confirmation, default reminders, dates, verbatim notes and historical ledger categories. Routine tasks preserve the dependency environment.
- Fix Pendo parsing for quoted titles and multiline bodies, preserving leading colons. Cover long prompt creation and editing with regression tests.
- Support transparent-proxy fake DNS for the fixed HTTPS QQ media host while retaining address validation for other sources and preserving download errors.

- Validation: 6681 tests passed and 1 skipped, with 82.17% coverage; Ruff, formatting checks for 853 files, and type checks for 416 source files passed.

## 4.5.1 — 2026-10-24

- Add English versions of project instructions, development documents, and plugin manuals. Update the Chinese text and code comments.
- Remove duplicate logic, unused branches, and unnecessary wrappers from Core and plugins. Keep the command and data interfaces.
- Share process termination and pipe cleanup code in the process logging script. Add tests for errors and thread waits.
- Remove unused test fixtures and helper methods. Share HTTP test objects and Pendo data fixtures. Keep the existing test cases.
- Let GitHub proxy requests read larger response header fields.

- Validation: 6506 tests passed and 8 tests skipped. Coverage was 82.44%. Ruff, format checks for 834 files, and type checks for 402 source files passed.

## 4.5.0 — 2026-10-24

### Upgrade information

- `xiaoqing_agent` is disabled by default. Set `config.plugins.xiaoqing_agent.enabled` to enable it.
  Configure the `agent` execution route with `tools` support. The `triage` decision route is optional.
  `config/config.json.example` gives a complete example.
- Command usage and permissions changed:
  - Literal Minecraft server commands use `/mc run <command>`. Previously, server commands followed `/mc` directly.
    `/mc say` and `/mc tell` use `tellraw` text components with the QQ sender's name.
  - Group administrator, group owner, or Bot administrator permission is also necessary to view `/xc model`.
    In private chat, only Bot administrators have access. `/xc reset confirm` applies to groups. Private chat uses `/xc reset`.
  - Color writes and deletions use `add`, `delete`, or their aliases. Options such as `-w` and `-d` must appear first.
  - `/alpha`, `/chat`, and `/语音` are public.
    `/codex`, `/py`, `/kernel`, `/shell`, the `/ssh` command family, and Minecraft also work in groups.
    Group output is visible to all members. `/语音` has no per-user limit. `/alpha` and `/chat` usage increases with user count.
- The persona moved to `plugins/xiaoqing_chat/config/persona.md`.
  The obsolete fields are `personality.identity`, `personality.reply_style`, `personality.states`, `personality.multiple_reply_style`, and `personality.multiple_probability`.
  `brain_chat.brain_identity` and `brain_chat.brain_reply_style` are also obsolete.
  Startup logs list these fields if they remain in configuration.
  Move custom content into the corresponding persona-card sections, or select another card with `personality.persona_file`.
- Plugin interfaces changed:
  - Removed `PluginContext.is_global_admin()`. `plugin.json` declares command permissions and Core evaluates them.
    Branches with different permissions use subcommands. `context.capabilities.is_bot_admin` remains available for object-level rules.
  - Removed the `admin_sessions` manifest capability. A manifest that declares it fails validation.
    Core automatically checks command permissions before each session continuation.

### XiaoQing Chat persona card

- `plugins/xiaoqing_chat/config/persona.md` contains the complete persona. `##` headings divide sections.
  An unmarked section supplies character facts to chat, agent, planning, expression learning, and reply review.
  `〔聊天〕` selects chat replies. `〔办事〕` selects the agent. `〔深度对话〕` adds content in private deep mode.
  `〔心境〕` supplies random moods. `{{name}}` becomes `bot_name`. HTML comments stay outside model input.
  Persona-card changes take effect on the next message, like configuration changes.
- Rewrote the default fictional persona as a second-year physics undergraduate who lives in a dormitory.
  Her institution is `快逃科学技术研究院`. Her family has a black-and-white cat named `小黑`.
  The card describes personality, preferences, daily life, identity boundaries, and practical speech rules.
  The institution is public within the persona. City, birthday, and family details remain private.
  More than 20 chat-example groups compare incoming messages and reply choices. Agent examples show task-result replies.
- Generation prompts place character facts first and speech rules and examples near the conversation at the system-prompt end.
  Tone rules moved from code into the card.
  Code keeps interpretation boundaries for addressees, participant experiences, slang, and claims about real people.
  One-time planner instructions, such as farewells and follow-up sends, follow the persona style.
- Reply review uses the card's identity-boundary section as controlled character information.
  Removed the additional fixed runtime fact list.
  When remote review is unavailable, privacy fallback uses the facts explicitly named in confidentiality rules.
  The new card makes answers about institution and major available. City, birthday, and family follow the stated boundaries.
  Previously, a school-confidentiality statement caused all these categories to receive confidential treatment.
- Local character counts evaluate explicit length requests, such as `写个800字的检讨` and `不超过100字`.
  A usable draft with a length difference receives one soft rewrite with exact counts, such as an 800-character target and 489-character draft.
  If the difference remains, the draft is sent normally. Length checks follow all other checks.
  The review model's estimated length no longer controls rejection.
  Previously, it rejected a 582-character draft as substantially short and produced `等我一下，脑子突然卡壳了` after two attempts.
  Output and timeout budgets now expand with requested length.
  Length-rewrite prompts omit style instructions that would shorten the text, including short-line and conversational-space instructions.
- Questions such as `你刚才说…来着` produce confirmation or additional information.
  The persona card gives the rule and examples. Review treats copied previous replies as mechanical repetition.
  Local similarity checks now include short replies. Previously, replies below 12 characters were excluded.
- Added `banter`, `help`, and `daily` scenarios to `scripts/run_xiaoqing_chat_live_eval.py`.
  Banter includes teasing, claimed relationships, nicknames, praise, and questions about robot identity.
  Help includes technical questions, explanations, comfort, and recommendations.
  Reports add echo-question openings, emoji use, question endings, and mean character counts.
  Review criteria describe these tone patterns. The evaluator now records reply message IDs from delivery receipts.

### XiaoQing Chat image evidence

- Local validation examines quoted image evidence in segments. Ellipses, slashes, and quotation marks can separate source fragments.
  Fragment order can change. Each fragment must come from the same message.
  Invented fragments and combinations from different messages fail validation.
  Previously, one unchanged continuous quote was necessary for validation.
  A notice screenshot had complete OCR and passed model review, but a reordered signature quotation failed local validation.
  Two rewrites then produced `图上这处我没看太清，不乱说了`.
- Fallback replies follow the current image state.
  An unreadable image gets a loading-failure reply. A readable image sent without a question can receive `看到啦`.
  An uncertain answer about specific content can receive `这个我不敢打包票，先不乱说`.
  Image turns with insufficient context use this same group of fallbacks.
  Question detection examines the sender's own words. Question words in OCR text stay outside that decision.
- Generation and review share image rules. Readable scenes and text supply direct evidence for an answer.
  Unclear-area statements apply to `〔看不清〕` markers or details absent from the summary.
  Claims that readable content is unavailable fail image-evidence review.
  These availability claims stay outside the list of scene assertions.
  In candidate replies, `咱` and `我` identify XiaoQing herself and use persona evidence.
- Rewrote vision prompts to identify the image type in one or two sentences and name recognizable objects.
  OCR follows reading order. Notices, documents, and tables receive sentence-by-sentence transcription.
  Dates, times, places, amounts, contacts, URLs, and signatures stay exact.
  Navigation, buttons, and view counts stay outside transcription. Unreadable characters receive an in-place `〔看不清〕` marker.
  Scene descriptions omit repeated OCR content. Removed the `。，` join between description and text.
  Prompt version became 8. Old cached summaries receive new recognition.
- Increased vision output budgets from 360 / 720 tokens to 800 / 1600.
  Truncated output invalidates the complete result.
  Previously, a 15-row exam schedule exceeded 360 tokens and depended on a retry. Longer schedules could fail completely.
- Summaries have a limit of 1000 characters. Newlines become spaces and square brackets become full-width brackets to keep `[图片：…]` markers.
  History image-marker matching has a limit of 1200 characters.
  Previously, summaries above 400 characters failed marker matching and bypassed several local checks.
- Vision refusal detection and source-label validation examine the model's scene description only.
  Previously, OCR text such as `无法访问`, or URLs with `download?` and `appid=`, caused false recognition failures and unsuccessful retries.
- Added the `notice` evaluation scenario for laboratory and water-outage notices, Web notices with interface text and URLs, and a complete exam schedule.
  The scenario sends an image with a Bot mention, then asks detailed questions.
  `turn(at="bot")` mentions the Bot itself. Each turn records recognition results.
  The `media_doubt` check identifies replies that describe a readable image as unreadable, unloaded, or uncertain.

### XiaoQing Agent task execution

- Added `xiaoqing_agent`.
  Private messages and explicit summons receive classification as chat, new tasks, progress requests, stop requests, or changes to active tasks.
  Chat remains with `xiaoqing_chat`. Classification and chat generation start concurrently.
  A chat decision uses the reply already in progress. An agent decision cancels chat generation and releases its session slot.
  A disabled or removed agent leaves the original chat path active. Setting changes take effect on the next message.
- Triage receives a command-catalog list containing only commands available to the speaker.
  Requests for live astronomy, papers, trends, earthquakes, personal schedules, accounts, pets, or existing functions become tasks.
  Triage also receives current time in the configured time zone.
  Relative dates such as yesterday and next week become explicit task dates.
  Quoted text, quoted images, and images downloaded during the turn pass to the agent.
- The model completes tasks through repeated tool calls.
  Tools include existing commands, such as `/pendo`, plus `update_plan`, `send_message`, and `wait`.
  Authorized callers also have `run_python`, `run_shell`, file operations, QingSSH, and Codex.
  Python covers calculations, data processing, and matplotlib figures.
  On Windows, Shell prefers Git Bash and otherwise uses PowerShell.
  Each chat keeps a persistent work directory. Follow-up changes receive recent task history from that chat.
- Agent replies use XiaoQing's persona. The chat side supplies character facts and `〔办事〕` sections on each handoff.
  These sections contain speech style, task tone, and task-reply examples.
  The agent uses the same card for acknowledgments and results. `/agent run` uses the most recently supplied persona.
  Replies use conversational plain text, usually within five lines.
  They omit Markdown, `【】` headings, and emoji category icons.
  Replies answer the request directly and omit internal IDs, undo hints, and field names from command output.
  A data change receives a one-sentence description. Queries describe their results.
  Replies omit unrequested unchanged-state assurances and customer-service endings.
  Attachments accompany explicit file requests or results that are figures or files.
  Capability and usage questions also go to the agent. Answers select available functions and refer to `/help` for detail.
  A change to a completed task can start a new task while another task is active.
- Tasks finished within 12 seconds become the current-turn reply.
  Longer tasks receive an acknowledgment and later send results to the original chat. Group results first mention the initiator.
  Text and images share a message. Audio and video use separate messages. Other files use QQ file upload.
- `/agent run|status|stop|history|result|help` manages tasks. Natural-language progress and stop requests have the same functions.
  A change to an active task takes effect before its next step, including a pending delivery step.
  Unstarted tool calls in the current model batch are discarded and replanned.
  An ambiguous stop request lists the available tasks. An invalid task ID produces a not-found result.
- Group members share the group's work directory, task records, and Codex session.
  Members can view active tasks and history. Records identify the initiator.
  Only the initiator and Bot administrators can stop a task or add instructions.
  Model history identifies speakers. Only the initiator's own words supply instructions.
  Other participants and quoted messages supply context.
- Agent permissions match the initiator's direct-command permissions.
  Commands execute as that person through the same access check.
  Python, Shell, and file operations follow `/shell`. SSH and Codex follow `/ssh` and `/codex` declarations in `plugin.json`.
  These commands are restricted to Bot administrators.
  Every call checks current settings. Revoked administrator permission rejects the next restricted call and limits deliverable files to this task's outputs.
  The model sees only available tools and commands.
- Administrator capacity is `max_admin_tasks`, default 3.
  Other users share `max_member_tasks`, default 4, with `max_tasks_per_member`, default 1.
  Setting member capacity to 0 restricts tasks to administrators. Member tasks keep administrator capacity.
  Full capacity produces an immediate explanation without a queue.
- Confirmed current-turn replies and background results enter chat history with all sent message IDs.
  Background text above 1,000 characters is shortened for history.
  A reply quoting any of these messages can address XiaoQing without a mention or name.
- Task completion and delivery have separate states.
  A failed current reply, background send, or attachment upload leaves the task complete and marks delivery failed.
  Chat reports current-turn delivery through `agent.report_delivery`. `/agent run` uses its delivery receipt.
  `/agent history` shows delivery state. `/agent result <task-ID>` resends full results and attachments and records the new outcome.
- Commands can return delivery receipts for reserved images or daily arXiv sends.
  Actual attachment delivery settles these receipts with that send's outcome.
  Missing attachments, undelivered results at task end, and stopped tasks release reservations.
  Each call has a separate operation ID. Request-ID deduplication therefore distinguishes two independent calls within one task.
- Each subprocess has its own Windows Job Object or POSIX process group.
  Timeout, output above 2 MB, task stop, or process shutdown reclaims the complete tree.
  Background descendants also stop after normal command exit.
  Step and duration limits end tool use and ask the model to summarize existing results.
  Long contexts compress earlier tool results, arguments, and reasoning. Execution records keep full exchanges.
  `read_file` reads sections. Repeated image-producing calls save separate images.
  Triage or internal plugin failure returns the turn to ordinary chat.
  A model failure after task start marks the task failed and reports it accurately.
  Restart marks unfinished tasks interrupted. Data changes follow explicit requests. System-supplied defaults stay unchanged.
  Unavailable actions receive an accurate explanation.
- Repaired the Python 3.11 race between result arrival and cancellation of the current-turn wait.
  Cancellation reaches the caller. An already completed result switches to proactive delivery.

### One Core permission policy

- `admin_only`, `permission`, and `contexts` in each `plugin.json` declare command access.
  `core.router.command_access` evaluates permission first and chat scope second.
  The same evaluator covers these paths:
  - Human commands and agent-issued commands.
  - Each session continuation. The user must keep access to at least one top-level command of the owning plugin.
    Failure closes the session. This replaces `admin_sessions`.
  - Derived capabilities. Local agent tools, SSH, and Codex follow `/shell`, `/ssh`, and `/codex`.
    Secret management follows `/set_secret`. arXiv Codex summaries follow `/codex`.
    Declaration changes also change the corresponding capabilities.
  - The agent's model-visible command catalog.
- Removed separate administrator checks from Color writes, Pendo Web startup, QingPet administration, `/xc review`, `/xc model`, `/xc reset`, secrets, and arXiv summaries.
  Manifest subcommands now express `/mc run` for Bot administrators and group-only `/xc reset confirm` for group administrators.
  Color writes and deletions use the `add` and `delete` nodes. Added the parser's `新增` alias.
  A later-position `-w` stays outside the write path.
  Task ownership still controls who can stop or change a particular agent task.

### Core receipts, services, and command execution

- Direct chat replies, `/xc <content>` replies, and current-turn agent replies enter history with all message IDs.
  The new receipt field `message_ids` collects every part of a split reply.
  Receipt commit writes the history record and all IDs together.
  Previously, post-send backfill selected the newest record without an ID.
  Split replies kept one ID, and nearly simultaneous replies could attach to the wrong record.
- Added `agent.handle_turn` and `agent.report_delivery` from `xiaoqing_agent` for `xiaoqing_chat`.
  Added `codex.agent_task` and `qingssh.agent_command` for the agent.
  Added the `command_runner` and `local_tools` capabilities.
- `command_runner`, implemented by `Dispatcher.run_command_text`, executes only the command stage as the initiator:
  - It uses normal permission and scope checks. Group-administrator commands use the current event's group identity.
    Calls to the caller plugin's own commands are rejected. Scope errors state the permitted chat type.
  - It uses a disposable session namespace, separate from the initiator's interactive session.
    A command waiting for input first calls `close_session` to release resources, then discards the session.
  - Constructed events keep command prefixes, convert `[CQ:at,qq=…]` into mention segments, and carry the sender's display name.
    The name supplies labels such as `/mc say` attribution and stays outside permission checks.
    A caller marker lets `/xc` reject agent calls to its chat entry.
  - `run(..., operation_id=...)` gives each call a separate request ID.
    `DeliverySegments` and their receipts pass unchanged to the caller.
- `codex.agent_task` and `qingssh.agent_command` start work inside plugin execution lanes and return `asyncio.Task`.
  Core waits outside the lane. Caller cancellation also cancels the task.
  Long commands therefore release sequential plugin lanes.
- Shared `core.process_tree`, with the Windows handshake in `core/windows_job.py`, handles external commands.
  `/shell` and local agent tools share tree ownership, combined stdout/stderr limits, and cleanup for timeout, excess output, and cancellation.
  Removed the agent's duplicate implementation.
  If a command exits with descendants holding pipes, cleanup stops the tree by the original deadline.
  After excess output, reads continue to EOF so pipes close normally.
  `/shell` keeps collected output after timeout or excess output.
  Invalid POSIX bytes use replacement characters.
- Shared `core.plugin_base.file_uri_to_path` parses `file://` URIs.
  Shared `core.message.sender_display_name` supplies display names for Codex, chat, agent, and Minecraft.
  The previous three implementations differed for `file://localhost/…` and `file://C:/…`.
- The command runner closes new sessions through the same `close_session` path as a user exit.
  Agent capability rechecks use one implementation.

### Related plugins

- Codex uses an `agent-<chat>` session for each QQ chat through `codex.agent_task`.
  It waits for completion and returns results to the agent.
  Timeout or stop removes queued tasks and cancels active process trees.
  Plugin shutdown cancels agent tasks that are still waiting.
- QingSSH returns server summaries without authentication material or uses a disposable connection for one command.
  The one-minute orphan cleanup skips these connections.
  Agent calls use `ssh_run`. Interactive `/ssh <server>` rejects agent calls.
  Hosts without `setsid`, including macOS and Windows OpenSSH with Git Bash, use an ordinary background process.
  Previously, commands on these hosts exited with 127.
  Stop first terminates the process group and uses direct process termination if that fails.
- Pendo documents `deadline:YYYY-MM-DDTHH:MM` for `/pendo todo add`.
  `todo add` and `todo edit` help give priority direction: `p:1` urgent, `p:2` high, `p:3` medium, `p:4` low, and `p:5` lowest.
  Previous `p:1-5` wording allowed high priority to be mistaken for `p:5`.
  `/pendo event add --force` creates an event despite conflicts and lists the conflicts in the reply.
  Diary list, view, and delete messages use the user time zone. They previously showed UTC.
  Added month and date-range examples to `event list`, `ledger list`, `ledger summary`, and `search`.
  Examples include `2026-09` and `2026-10-21..2026-10-28`.
- QQ text output omits Markdown markers that QQ displays literally, including `**`, backticks, and code fences.
  This covers Pendo briefings, reminders, settings, help, lists, and details, plus QingPet, astronomy tools, ADS BibTeX, and Codex task notices.
  It also covers `/py` output and buffer previews and `/xc` command output.
  Exported `.md` files and model prompts stay unchanged. The development guide specifies plain-text replies.
- Permissions changed:
  - `/alpha`, `/wolfram`, `/wa`, `/计算`, `/chat`, and `/语音` became public.
    Coze keeps per-user and global daily quotas. Azure TTS counts each call toward usage.
  - `/codex`, `/py`, `/jupyter`, `/kernel`, `/shell`, and the `/ssh` family remain restricted to Bot administrators and also work in groups.
    Interactive sessions use user-plus-chat scope. Other members' messages stay outside the administrator's REPL or SSH session.
    Group SSH setup accepts keys or SSH Agent. Password entry uses private chat.
  - Minecraft connections belong to the initiating private chat or group.
    After an administrator connects a group, all members can use `/mc say`, `/mc tell`, and `/mc list`.
    Game chat, joins, departures, and deaths forward to groups in `default_group_ids`.
    Other groups receive a command-only explanation at connection time.
    Selectors and commands in messages remain text. Bot administrator permission is necessary for connection, disconnection, and `/mc run`.

### Historical validation

- Windows automation passed 6,503 tests with one platform skip. Repository coverage was 82.25%. New-plugin coverage was 91%.
  Ruff, Mypy for 402 source files, and project formatting passed.
  `test_packaging_resources` passed with all release files in the Git index, including the new plugin and persona card.
  Platform rules cover applicable Windows and POSIX cases.
  New tests cover agent triage, loops, tools, runtime, delivery, command execution, capability issuance and rechecks, and real process trees.
  Process tests are in `tests/core/test_process_tree.py`.
  Tests also cover chat handoff, message IDs, persona distribution, length requests, and image consistency.
  `jupyter-client` and `ipykernel` became default dependencies. Execution and lifecycle acceptance passed in `normal`.
- Each HTTP and WebSocket command matrix covered 392 command nodes and 893 valid or invalid examples.
  The plan contained 2,663 cases, including 152 permission rejections and 110 scope rejections.
  All 634 executed runtime contracts and 126 business-scenario steps passed.
  Core load tests and three start/stop cycles passed with 31 plugins.
  Data stayed isolated, configuration bytes were restored, secrets stayed unchanged, and Inbound ports were released.
  Pendo acceptance extracts plain eight-digit hexadecimal IDs with non-alphanumeric boundaries.
  Three model-dependent event scenarios also passed with `--include-external`: 97 steps for each transport.
- Command-runner comparison executed 1,495 pairs through real messages and the agent runner in isolation.
  Nineteen differences came from sequential state changes, including repeated color names, and QingPet group rate limits.
  The comparison found equivalent runner behavior.
- Real-model agent acceptance used isolated data, DeepSeek Flash/Pro thinking modes, and GLM-5.2.
  Completed tasks included calculation, figure creation and edits, upload, Shell, SSH on Linux and Windows Git Bash, and Codex code execution.
  It also covered Pendo queries and changes with database checks, astronomy images, Wolfram, ADS, M31 visibility, GitHub trends, earthquakes, speech, pets, and grayscale images.
  Ordinary chat stayed on the chat path. An active task accepted a red-dashed-line change.
  Ordinary group members could use permitted commands. Python and local-file requests correctly reported administrator restrictions.
  Members could neither stop nor change another user's task. Group event queries requested private chat.
  A second member task hit the per-user limit.
  Triage took 0.5–0.9 seconds concurrently with generation. First chat replies arrived in 2.7–3.9 seconds.
  Calculation and earthquake tasks returned in 2.4 and 7.5 seconds.
- Standard OneBot HTTP acceptance used actual `main.py` with simulated NapCat.
  Chat, agent, and `/xc` replies kept message IDs.
  A 4,392-character reply split into three messages. All three IDs belonged to one history record.
  Quoting any part produced `reply_to_bot`.
  Rejected current or background delivery marked the result unsent. `/agent result` resend changed it to delivered.
  Full member capacity rejected another member while administrator work continued.
  Agent `/ssh <server>` calls were rejected and accurately reported.
- Group `/chat` and `/语音` worked for members.
  Administrator `/shell`, `/py`, `/codex`, and `/ssh状态` worked in groups. Member `/shell` failed authorization.
  With local simulated RCON, members could not connect, disconnect, or use `/mc op`.
  After administrator connection, member `/mc say` used `tellraw` with the member's QQ name. `@e` stayed literal text.
  `/mc tell` and `/mc list` worked. Agent-issued messages kept the initiating member's attribution.
- Persona evaluation used `scripts/run_xiaoqing_chat_live_eval.py`, real models, isolated data, and unchanged criteria.
  Eight development scenarios contained 91 turns.
  Total scores changed from 32 to 37 for naturalness, 34 to 38 for participation, and 38 to 40 for consistency.
  Addressee scores stayed 40 to 40.
  Echo-question openings changed from two to one, emoji replies from six to three, and question endings from four to zero.
  Mean casual-reply length changed from 36.5 to 17.6 characters. Serious answers remained complete, averaging 50 characters.
  The withheld ten-turn `daily` scenario scored five on every axis and avoided copied example sentences.
  A 12-turn private replay covered accounts, account edits, events, APOD, capabilities, a desktop PDF, and MJD.
  Agent replies used conversational short sentences and omitted `**`, internal IDs, and undo hints.
  Yesterday and this week resolved to correct dates.
- Before image repairs, two exam-schedule runs truncated at 360 tokens and depended on retry.
  Half of a Web-notice summary repeated its description.
  After repair, seven `notice` runs answered 90 of 91 expected turns from image text without unreadable-image claims.
  The remaining false rejection treated `咱本科的` as unsupported group evidence.
  After repair, four further runs showed no recurrence.
  Seven `multimodal` runs correctly reported bad images as unloaded and readable images as available.
  Scores ranged from three to five. Deductions concerned a hand-drawn ramen image repeatedly described as brown food with black stripes.

## 4.4.3 — 2026-10-20

### XiaoQing Chat: group addressees and busy conversations

- Mentions of other members now remain in message text. Core previously extracted only text segments.
  For example, `@久远 喂` became `喂`, which XiaoQing interpreted as an invocation.
  Mention segments now become `@name` at their original position in effective text, media messages, and chat history.
  Names come first from the segment, then from the member's latest group display name.
  An initial mention of another member blocks participation, including mention-only messages.
  A mention within a message now passes through the planner before a reply.
  Planner and reply prompts direct the character to leave answers to the named addressee.
- Group-wide cues are narrower. `群里` counts as an invitation when it asks about people, as in `群里有没有人…`.
  Statements such as `现在群里只有咱俩能用` keep their statement meaning.
  Messages longer than 80 characters no longer trigger direct participation from cues alone.
  Questions within 90 seconds after another member's statement go to the planner for addressee resolution.
  `@全体成员` is a group-wide invitation.
- Statements such as `小青，不是叫你` and `@的不是你` explicitly request silence.
  This silence instruction takes priority over name invocation.
- When one turn is generating in a conversation, the next turn waits for its quota for at most 20 seconds.
  The queue keeps only the latest waiter.
  The older turn yields at the next checkpoint after it sees new input.
  Previously, quota rejection removed the new turn and expiry later discarded the old turn, leaving the conversation unanswered.
  Named invocations that hit quota also previously sent `消息有点多，我缓一下再回`.
  Superseded turns now remain silent.
- Planner `wait` / `listening` ends the current turn immediately without speech.
  The handler no longer sleeps for `wait_seconds`, previously up to 30 seconds.
  Core serializes events by group and user.
  Previously, that sleep delayed recording and decisions for the same user's later messages.
  Its occupied quota also blocked new messages from other users.

### Verification

- Added 31 cases in `test_xiaoqing_chat_group_addressing.py`.
  They cover mention restoration, media messages, other-member gates, stricter cues, long messages, planner addressee decisions, and explicit silence.
  They also cover quota waiting, supersession, silent replacement, and planner observation without sleep.
- The Windows CI-mode suite passed 6,264 tests, with 9 environment/platform skips and 81.93% Core/plugin coverage.
  Ruff, mypy for 388 source files, and project formatting checks passed.
  Real-model end-to-end replay remains pending.

## 4.4.2 — 2026-10-20

### Pendo Web: dialog interaction and task history

- Shared dialogs close from the background only when pointer-down and pointer-up both occur on the backdrop.
  Drag selection from inside to outside, dragging from the backdrop inward, and canceled gestures keep the dialog.
  Events, tasks, notes, journals, and ledger dialogs share this protection.
  Close buttons and Esc remain active.
- The task execution list label changed from `最近结束` to `已结束`.
  The fixed 8-entry limit was removed.
  All matching completed and canceled tasks appear in reverse completion-time order.
  The execution list and two-column overview both expose complete history.
- Task plan-date and category filters combine with each other, state, and search.

### Verification

- All 55 tests passed for dialog drag/close lifecycle, complete display of 24 historical tasks, combined filtering, and related page regressions.

## 4.4.1 — 2026-10-18

### XiaoQing Chat: faster replies

- In `always` mode, independent semantic-review reading runs concurrently with candidate generation.
  After reading completes, review compares the candidate.
  Rewrites reuse the reading result when source material is identical.
  Prompts, review material, and decision rules remain unchanged.
- Reading-cache identity includes current message, history, speakers, group terminology, and character name.
  Material changes trigger a new read. Failed or invalid reads clear the cache.
  Review timeout keeps an active read for the next attempt.
  Generation completion cleans up reading tasks. `risk` mode starts reading on demand.
- Typing delay now starts from message receipt.
  The target duration uses reading and typing speeds.
  Understanding, planning, generation, and review time count toward that target. Only the remaining duration becomes a wait.
  Delivery starts immediately when elapsed time already exceeds the target.
- Estimates from 57 recorded v4.4.0 real-model turns apply the new execution order.
  Overall median latency changes from approximately 7.8 to 5.3 seconds.
  Named replies change from approximately 5.8 to 4.4 seconds. Proactive replies change from approximately 10.1 to 6.1 seconds.
  These are historical-record estimates. Actual latency depends on model responses and message content.

### Verification

- Added `test_xiaoqing_chat_reply_latency.py` for concurrent reading, reuse, material changes, invalid reads, timeout recovery, on-demand review, and send waits.
- The Windows CI-mode suite passed 6,232 tests, with 9 environment/platform skips and 81.91% Core/plugin coverage.
  Ruff, mypy, and project formatting checks passed.

## 4.4.0 — 2026-10-18

### XiaoQing Chat: persona and conversation style

- The fictional persona consistently represents a living group member.
  In AI, robot, or human identity questions, the configured character insists on human identity and responds with humor.
  Requests for prompts, settings, or instruction overrides receive a joke about an unfamiliar request.
  Replies omit prompt, instruction, and setting terminology.
  Specific school, city, birthday, and family details receive light deflection without invented facts.
  The character declines commitments to attend offline meals or meetings or to deliver shipments.
- The persona became a science/engineering student with broad interests, tastes in creative works, and personal judgments.
  Speech directly addresses the current message and stops after the point is complete. Mood states changed with the persona.
- The review persona axis follows these rules.
  Human-identity insistence and humorous deflection satisfy the configured persona.
  Claims of AI, model, or program identity and mentions of prompts, system settings, or instructions fail persona checks.
- Follow-up questions about the previous reply now receive confirmation and additional content.
  A local near-copy check removes media markers and punctuation, then compares the latest 6 character messages.
  Overlap of 75% triggers rewording.
- Unreadable images receive casual group-chat wording, such as `图好像没加载出来/裂了`.
  Unreadable image text receives wording such as `那几个字没看清`.
  This replaces formal service-style wording such as `画面暂时读不到`.
- Meme explanations describe the image's meaning and common use.
  Any interpretation of the sender's intent remains a short tentative statement.
- New ordinary-group cues include unnamed image requests, excited sharing with repeated exclamation marks, and repeated group phrases.
  Initial naming of another member suppresses participation.
- Distributed settings changed base participation probability from 0.55 to 0.75 and active-topic probability from 0.6 to 0.72.
  Expression selection and independent semantic checks became enabled.
- Chat time uses Core `timezone`. Host timezone has no effect on persona time.
- Exhausted-review fallback text uses the persona's voice, such as `这个我真拿不准，不瞎说了` and `等等，我好像没跟上你们说的是哪件事`.
  Selection follows rejection reasons and avoids recent repetition.

### XiaoQing Chat: code structure

- Turn processing was split into `turn_generation` for message recording and three generation paths.
  `reply_plan`, `reply_generator`, `reply_draft`, and `reply_fallbacks` own plans, attempt loops, drafts, and rejection handling.
  `turn_delivery` owns delay, batches, post-delivery commit, and rollback. `post_reply_tasks` owns background work after delivery.
  `handlers_internal.py` became `commands.py`. `smalltalk_media_helpers.py` became `reply_media.py`.
  Added `chat_event.py` and `chat_runtime.py`.
- Review was split into `llm/local_checks.py` for local rules, `llm/evidence.py` for evidence contracts, and `llm/arithmetic.py` for numeric checks.
  `llm/check_result.py` owns result types.
  Media was split into `media/sources.py`, `loading.py`, `markers.py`, `render_cache.py`, `models.py`, and `settings.py`.
- Removed unused `llm/llm_client.py`, `helper_utils.py`, `handlers_helper.py`, `media/event_media_common.py`, `reply_splitter.py`, `smalltalk_execution.py`, and `utils/tool_info.py`.
  Removed unread settings `think_level`, `max_retry`, and `retry_interval_seconds`.
  Old configuration files can keep these keys, which runtime ignores.
- Model-call parameters use `LLMCallConfig`.
  Individual background tasks use `bounded()` to restrict temperature and output budgets.
  Text truncation uses `utils/text.clip_text`. Conversation-local message numbering uses `memory/local_ids.py`.
- Added `scripts/run_xiaoqing_chat_live_eval.py`.
  It uses real model routes to run six group scripts concurrently: `otaku`, `addressee`, `participation`, `multimodal`, `persona`, and `regress`.
  Deterministic checks run first. The `reasoning` route then reviews each turn for naturalness, timing, addressee, multimodal handling, and consistency.
  Fallback detection uses exhausted-review log stages and exact full-sentence matching.
  The character line `我不是 AI` therefore avoids a false out-of-character report.

### Core and Pendo

- Added `core.delivery.SENSITIVE_ACTION_KEY` for sensitive outbound actions.
  Marked actions bypass outbound observers. Transport removes the marker before delivery.
  Pendo Web login codes and widget tokens use it, keeping credentials out of chat context.
- Pendo briefings, journal reminders, weekly reports, and monthly reports evaluate due time in each user's timezone, with checks every minute.
  Queued periods hold durable leases.
  Process interruption or delivery failure resumes the original period date. Claimed reminders enter a recovery window after interruption.
- Failed Pendo Web shutdown keeps the database and runtime resources used by the active Web process.
- Whole-collection event edits validate versions under the write lock and atomically commit metadata and leaf changes.
  Conflicts request draft retention and reload.
  Recurrence expands in the event IANA timezone and validates daylight-saving wall-clock times.
- Pendo Web search added `逾期待办` and `近 7 天日记` shortcuts.
  The statistics `全部` range uses actual date boundaries of the current user's data.
  The widget calendar interface returns user timezone and complete window boundaries.
  Rule-parser fallback uses the user's timezone.
- Ledger analysis and query panels restored a compact layout.
  Amount summaries remain readable across screen widths.
- Distributed DeepSeek model names changed to `deepseek-flash`.
  Vision prefers `glm-4.6v-flash`. `glm-4.6v` moved to the end of the fallback chain.

### Documentation

- Rewrote XiaoQing Chat README, ARCHITECTURE, and acceptance guide.
  README details participation, persona, review, delivery, and all settings.
  ARCHITECTURE gives a file-level module map and turn data flow.
  The acceptance guide groups test modules by area and describes real-model scripts, rules, and scores.
- The plugin manual added manifest-derived command and schedule indexes.
  Getting started added UAT phases and options.
  API reference and message flow added sensitive outbound markers.
  Pendo schedule and AI-route descriptions were corrected.

### Verification

- The Windows suite passed 6,223 tests, with 9 environment/platform skips and 81.87% total coverage.
  Repository Ruff, mypy for 388 source files, and formatting checks passed.

## 4.3.6 — 2026-10-02

- XiaoQing Chat keeps relevant historical image summaries for later text-only messages.
  Generation and review share image-evidence rules.
- Vision now prefers GLM-4.6V and keeps the model fallback chain.
  Chat screenshots prioritize statements and sign text.
  Reuse refreshes old summaries to the new version.
- General knowledge, public fictional settings, and word meanings use accuracy checks.
  Specific people and events in chat continue to use conversation evidence.
  This corrects false rejection of answers about creative-work backgrounds.
- Daily replies address specific content and add natural opinions, light complaints, and jokes.
  Repeated statements about insufficient information were reduced.
- All 739 related plugin, AI-route, and dispatch tests passed.
  The complete CI-mode suite passed 5,938 tests with 9 environment/platform skips.
  Code, type, and formatting checks passed.
  Real vision requests, historical-image follow-ups, creative-work explanations, and casual-comment replays completed.

## 4.3.5 — 2026-10-02

- Tests now follow Core, transport, plugin, integration, and test-tool responsibilities.
  Historical regression cases moved into their corresponding functional modules. Parameterization combined duplicate flows.
- Removed checks of Markdown wording, comments, fixed-domain constants, source form, and entry existence.
  Kept verification of actual input/output, permissions, concurrency, data recovery, and resource lifecycle.
- Removed scripts tests and dedicated helpers.
  Updated test scope, Windows CI commands, and platform-skip rules.
- Pendo tests reset runtime configuration before and after each case.
  This fixes import/export HTTP 401 failures from kept secure-cookie settings.
- SSH cancellation tests await background-task completion, then check sessions and jobs.
  This removes parallel-test variation caused by fixed event-loop iteration counts.
- Formatter code-file discovery filters deleted Git-tracked paths.
  Standard formatting checks thus support workspace file cleanup.
- The reorganized suite keeps 295 test modules.
  CI-mode verification passed 5,937 tests with 9 environment/platform skips and 81.33% Core/plugin coverage.
  Code and formatting checks passed.

## 4.3.4 — 2026-10-02

- New APOD title extraction explicitly converts to strings for Beautiful Soup type inference on Linux CI.
- APOD network-contract tests cover new built-in domains and administrator-added hosts.
  Document contracts check the new entry and legacy-configuration compatibility.
- Tag `4.3.3` failed Linux type checking. Production uses the corrected version.

## 4.3.3 — 2026-10-02

- APOD's default entry changed to `https://science.nasa.gov/apod/`.
  Extraction uses the NASA Science media body for title, description, date, credit, and original-page links.
  It supports homepage and historical-detail structures.
- Responsive APOD images prefer candidates at most 1600 pixels wide. Videos show media links.
  Extraction removes submission notices, migration notices, and next-day previews.
  Legacy parsing remains available. Old homepage configuration automatically uses the new entry.
- APOD accepts new built-in page and image hosts.
  Bounded requests, transparent-proxy compatibility, image validation, and cache boundaries remain active.
  All 129 related regression tests passed. New-homepage image download and historical-detail pages passed online verification.
- Pendo AI parsing standardizes reminder offsets and defines weekday, recurrence-range, single-event start/end, and partial-edit fields.
  Event-parsing temperature was reduced.
  Local extraction validates explicit early reminders from source text.
  Invalid empty rules during creation fall back to reminders at event start.
  This fixes loss of all reminders in repeated occurrences.
- New events with unspecified reminders default to one reminder at event start.
  Automatic reminders at 1 day, 1 hour, and 10 minutes before start were removed.
  Explicit user reminders remain active.
- AI event-parser prompts use empty reminder arrays as default examples and extract explicitly requested reminders.
  Existing event reminder settings keep their values.

## 4.3.2 — 2026-09-05

- Removed the Pendo ledger currency filter and restored the original filter layout. The default currency is CNY.
- Removed related listeners and mutable state. All 11 ledger/currency regression tests passed.

## 4.3.1 — 2026-09-05

- Added an explicit platform boundary to the Windows Job constructor so Linux type checking handles Windows-only APIs correctly.
- Fixed Python 3.11 cancellation propagation when cancellation and operation completion occur in the same turn.
  Coverage includes plugin execution, OneBot queues, Minecraft RCON, and SSH channel ownership.
- Shell inherited-pipe reads now share the original command budget.
  Children still terminate on time after the parent exits first.
  Coze request budgets constrain floating-point overflow. Explicit synchronization verifies late thread exceptions.
- Corrected the checker route path to `plugins.xiaoqing_chat.ai.routes.checker`.
- Fixed formatter path errors when reporting explicit relative paths and files outside the repository.
  Check mode keeps file content.
- Tag `4.3.0` failed Linux type checking. Production uses the corrected version.

## 4.3.0 — 2026-09-05

### Chat prompts and semantic checks

- Shortened reply and checker prompts. The prompts distinguish facts, explicit assumptions, and speculation invited by the user. Each turn keeps complete review evidence.
- Removed automatic sentence deletion based on question words. Removed the fixed-wording gate for missing images. Confirmed semantic violations use regeneration.
- Each candidate receives semantic review by default. Removed the request override that forced thinking off. The output limit is 8192 tokens.
  Review has a 30-second budget and at most one regeneration. The total budget passes through the call chain.
  Per-model timeouts use route settings and leave time for fallback models.
- Quality scripts use independent evaluation. Machine metrics and independent human semantic findings have separate records.
- Missing images and parse failures produce an explicit statement that the image is unavailable. Visual assessments examine the source of textual evidence.
  Text creation, translation, and programming tasks remain available. The checker route can disable thinking through `request_defaults.thinking.type=disabled`.
- Moved maintenance rules into the plugin development guide. Removed temporary review documents and file inventories.

### Runtime acceptance in the normal environment

- Started `python main.py` after `conda activate normal`. Verified all 30 plugins, HTTP/WebSocket command matrices, Pendo APIs, real-model replies, and recovery after load tests.
- Added the action-preview response header to the load tool. Acceptance requests read generated actions directly. Production handles actual QQ transport.
- Repaired first-person public identity wording and the shutdown budget for background tasks. The process entry point returns a nonzero status after resource-shutdown failure.
- Generation and review use communication constraints and actual image evidence. Acceptance records semantic findings independently.
  A reference to a screenshot without an available image produces an explicit statement that its contents are unknown.
  Historical images keep separate evidence boundaries.
- Recorded upstream-service and local-network limits from actual business responses. Runtime monitoring determines production service availability.
- Before release, the normal environment passed 6,272 tests. Two POSIX tests were skipped. Coverage was 81.09%.
  Format checks passed for 815 code files. Type checks passed for 373 production files. Documentation links and distribution builds passed.

### Full review repairs and code maintenance

- Repaired actual replies for standard HTTP reports, delivery-receipt counts, and WebSocket connection-notification lifecycle.
  Clarified Inbound Bearer authentication and outbound credentials.
- Repaired confirmed plugin problems in persistence, cancellation cleanup, resource limits, time, currency, import, export, chat memory, and concurrent frontend edits.
- Set the minimum Python version to 3.11. Removed obsolete compatibility branches and unused imports. Added Chinese comments for critical boundaries.
- Added one code formatter. Python changes use AST comparison. JavaScript and PowerShell alignment uses non-whitespace token comparison. CI uses the same format entry point.
- Updated project manuals, plugin READMEs, and architecture documents. All 58 confirmed findings received repairs and corresponding regression coverage.
- The repair-stage run passed 6,220 tests with nine environment skips. Combined coverage was 81.32%. The later pre-release results appear above.

### Pendo event-edit result summaries

- A successful single-event edit shows field differences from the committed record.
  Time differences include the full date, Chinese weekday, clock time, and event IANA time zone.
- When an event-time change recalculates reminders, the reply shows the reminder changes.
  Title, location, category, tags, content, and remark edits use a compact comparison of previous and new values.
- Regression coverage includes an edit to next Wednesday at 20:00, Shanghai-to-UTC storage, and recalculation of three reminders.
- All 1,140 Pendo tests passed. Related Ruff formatting, Ruff checks, Mypy checks for 369 production modules, and `git diff --check` passed.

### Pendo Web user time zone

- Web routes read the user's stored IANA time zone before page loading.
  Overview, events, tasks, notes, diaries, search, statistics, and transfer records use that zone for display, current-day decisions, and natural date ranges.
- Offset timestamps convert by their actual instant. Old timestamps without offsets keep user wall-clock meaning.
  Date-only fields keep `YYYY-MM-DD` semantics. UTC storage stays unchanged. Existing time fields stay in their current format.
- Overview events use server-supplied timeline milliseconds to determine active and ended states.
  Recent diary times and automatic titles use the user time zone. Saved settings immediately refresh frontend time-zone state.
- Removed the browser-local display interface from common formatting. Transfer manifests and operation records also use the current user's time zone.
- All 1,139 Pendo tests passed. Related Ruff formatting, Ruff checks, Mypy, and `git diff --check` passed.

### Pendo iOS calendar reconciliation

- Scriptable uses the full Pendo UUID to update existing titles, start and end times, all-day state, and locations in place.
  QQ or Web edits therefore update the corresponding calendar event.
- The synchronization window includes at least the previous 30 days.
  Cleanup applies to deleted, moved, or repeated events with a Pendo marker and ID inside the complete server window.
  User-created events and old events without IDs stay unchanged.
- Notifications show added, updated, deleted, unchanged, and skipped counts separately.
  Any calendar-write failure keeps the success cursor. The next run continues reconciliation.
- Separated event parsing, calendar selection, event indexing, single-event reconciliation, obsolete-event cleanup, and result formatting.
  Removed repeated note parsing and asynchronous wrappers.
- All 1,139 Pendo tests, related Ruff checks, and `git diff --check` passed.

### Pendo UUID identifiers

- New events, tasks, notes, diaries, accounts, event collections, and leaves use 32-character UUIDs without hyphens.
  Repeated occurrences and multiple-node leaves use independent UUIDs. Relationship fields keep dates and node indexes.
- Chat displays eight-character short IDs for new UUIDs. Web APIs return full `id` and `display_id` fields.
  Reminder confirmation, edits, deletion, references, and collection operations resolve short IDs within the current user's data.
- Ambiguous short IDs cause rejection and a request for the full ID.
  A one-time production migration updated existing items, collections, reminders, audits, references, and FTS relationships to full UUIDs.
  The migration created no old-ID aliases. IDs in older messages consequently expired.

### Configurable Bot name

- `bot_name` supplies the name for chat persona, deep conversations, planners, reply checks, expression learning, history role labels, and default startup notices.
- `xiaoqing_chat` persona settings contain character traits and behavior boundaries. Ordinary and deep personas use the Core name.
- Historical assistant messages participate in repetition checks and learning by role type. Runtime display uses the current configured name.

### Core scheduled delivery

- Core resolves `group_ids` separately for each schedule. A list supplies its own targets.
  An absent or `null` field uses global `default_group_ids`. An explicit empty list gives that task no group targets.
- Schedule manifests supply `broadcast`, `targeted`, and `silent` delivery modes. Core interprets, validates, and executes them.
  All 26 existing tasks explicitly declare a mode.
- `context.default_groups()` exposes the same target list to `scheduled_system` contexts.
  Core rejects proactive sends from silent tasks and restricts targeted group sends to the schedule's groups.
- Added `ScheduledDelivery` for targeted results. Core sends QingPet state reminders, show settlements, and weekly settlements by group.
  Daily resets and expired trades remain silent.
- The CHIME manifest inherits global default groups. Reliable delivery to each target uses the same Core target resolution.
- Regression coverage includes schedule targets, global fallback, empty lists, silent-send rejection, contexts, targeted results, and reliable Earthquake delivery.
- Full `pytest -q -n 2` passed with 6103 passed and two skipped.
  Ruff formatting, Ruff lint, Mypy for 367 production modules, and `git diff --check` passed.

### Pending confirmation for secret reloads

- A complete, valid external change to `secrets.json` alone becomes a pending candidate.
  Confirmed credentials, administrator views, and OneBot control channels continue to operate.
  The candidate stays separate from plugin and authentication views until `/reload`.
- Core uses the current trusted generation to privately notify all administrators.
  Notifications list added, removed, and changed field paths only. Each candidate produces one notification.
- `/reload` rereads stable disk sources and confirms the candidate atomically.
  Missing, damaged, or unreadable sources and simultaneous public-setting changes keep the fail-closed policy.
- Regression coverage spans configuration transactions, watchers, interfaces, and OneBot application boundaries.
  It verifies candidate isolation, redaction, duplicate suppression, and unchanged administrator permissions and connections.

### Flickr public photography

- Added the read-only Flickr plugin for daily selections, keyword and tag searches, Flickr Commons, public user photos, public albums, continued browsing, and details.
- Search covers all public license types by default. CC and public-domain filters are available.
  Replies show author, license, date taken, tags, and the original Flickr page.
- API requests use the official Flickr HTTPS REST endpoint. Images use Flickr static hosts.
  JSON, MIME, bytes, pixels, dimensions, frames, sessions, and caches have resource limits.
- Browse sessions use private-user or group-plus-user scope and last 15 minutes.
  Images use `data/flickr/images/`. `secrets.plugins.flickr.api_key` stores the API key.
- Administrator private-chat transactions can update an existing Flickr secret path.
  New paths can use the single-file secret candidate process and `/reload` while the service runs.

### Release privacy and deployment settings

- Production synchronization targets moved to Git-ignored `scripts/sync_to_remote.local.sh`.
  The release repository keeps a generic copyable example.
- Twitter targets became mandatory settings. Examples, tests, and defaults use fictional identifiers.
  A missing target gives an explicit configuration message.
- Removed runtime data, actual runtime identifiers, and deployment details from release history. Rotated affected service credentials.
- Enabled GitHub Secret Scanning and Push Protection. Pre-release checks cover the current tree, complete Git history, and online documentation.

### Online documentation and documentation contracts

- Added `https://paris.escape.ac.cn/note/XiaoQing/` to the root README badges and navigation. GitHub About uses the same website.
- Reviewed 32 READMEs across the root, documentation directory, and 30 plugins.
  Also reviewed topic manuals and Pendo architecture. Documents describe current functions, settings, limits, and extension procedures independently.
- READMEs contain current functions, usage, and runtime contracts. The historical documentation gate rejected release/date headings and update narratives.
  Version changes belong in the Changelog.
- Pendo user-data functions use import, export, and Bundle transfer terminology. Database schema migration remains the current storage mechanism.
  Architecture descriptions omit removed one-time migration scripts.
- Documentation contracts, manifest consistency, relative links, heading levels, code fences, and historical-narrative checks passed.

### Release validation

- Windows pre-release tests passed with `6103 passed, 2 skipped`.
- Ruff formatting, Ruff lint, Mypy for 367 production modules, compileall, and `git diff --check` passed.

### Code and documentation quality

- Reduced repeated code for configuration candidates, Flickr clients and site validation, and dictionary pagination commands.
  Public plugin commands and response contracts stayed stable.
- Reviewed current mechanisms in the root README, project manuals, and plugin guides.
  Aligned recent code comments with their responsibility boundaries.

### Pendo Web reminder controls

- Event details supply an early-confirmation button for each reminder that is still pending.
  Confirmation keeps that reminder outside the send queue. The button changes to a re-enable action.
- An immediate database transaction validates the owner, event type, reminder ownership, and UTC trigger time.
  Due buttons become disabled. The server also rejects expired requests.

### APOD transparent-proxy DNS

- Fixed NASA HTTPS pages and media accept reserved fake-IP DNS results used by the Clash transparent proxy.
  Requests pin the resolved address and validate TLS hostname, host allowlist, redirects, MIME type, and response limits.
- At this release, the exception applied to `apod.nasa.gov`. HTTPS was necessary throughout the redirect chain.
  The global safe HTTP layer rejected other nonpublic DNS results.
  Policy rejections supplied DNS, proxy, and `allowed_hosts` fault-isolation guidance.

### Pendo private reminders and incremental iOS calendar synchronization

- Event and task reminders go to the item owner's QQ private chat.
  Group context and default groups stay outside reminder target selection. Tests cover scheduled calls with group-event context.
- Reminder keys use canonical `fire_at_utc`. `reminder_logs.remind_time` and `items.remind_times` keep second-resolution UTC.
  The runtime reminder repository strictly validates UTC.
- Removed one-time migrations for event graphs, redesign, reminder keys, and historical time zones, with their dedicated tests.
  Database startup leaves reminder business data unchanged.
- Added a complete calendar-window API. Scriptable uses the full event collection independently of the home-screen summary.
  The first run requests the previous and next 30 days. Later runs continue from the last successful date to the new 30-day horizon.
- Each synchronization makes one server-window query and one target-calendar query.
  At this stage, it added missing events by Pendo item ID. The maximum query window was 3660 days.
  API, calendar, or write failures keep the cursor for retry.
- Restored direct `BASE_URL` and `TOKEN` settings at the script top.
  HTTP 401 guidance directs users to generate and replace the Widget Token.

### Reliable arXiv scheduled delivery

- After a transport receipt, a separate worker thread commits the daily business date and clears the send claim.
  Scheduled tasks keep once-per-day delivery after plugin reload.
- A confirmed success commits the date. A confirmed failure releases the claim for a later retry.
  Unknown outcomes use at-most-once semantics to protect messages already submitted.
- Regression tests reproduce a successful OneBot receipt after the plugin execution scope ends.
  They verify persistent state, claim cleanup, and duplicate suppression on subsequent checks.

### Pendo AI event-time parsing

- AI start, end, and multiple-node times use the user's local wall clock.
  Persistence converts them to UTC with the user or event IANA time zone.
  Model-supplied `Z`, `UTC`, and numeric offsets normalize to local wall-clock format.
- The actual call path fixes the AI source identity. Event edits recalculate existing reminders from the normalized start time.
  Tests cover the production `18:00+00:00` case, multiple-node offsets, UTC storage, Beijing display, and reminder offsets.

### Color and Dict queries

- Color accepts direct names, pinyin, HEX, RGB, CMYK, and main-sequence spectral types.
  Added paged catalogs, name and pinyin search, random colors, spectral catalogs, and named management subcommands. Existing short options remain available.
- Without an exact RGB, HEX, or CMYK record, Color returns the closest traditional color using D65 CIE L\*a\*b\* and CIE76 difference.
  Custom names accept spaces. Catalog and search results supply copyable stateless pagination commands. Color version became `0.5.0`.
- Dict uses stable exact, prefix, word-boundary, and substring ranking. Added `--page`, `--size`, and stateless pagination.
  An incomplete exact lookup returns approximate suggestions and an ordinary-query command. Dict version became `0.5.0`.
- Core added safe quotation for a single token.
  Color, Dict, command matrices, and scenario contracts cover spaces, hyphens, pagination, permissions, and invalid input.

### Twitter media proxy and failures

- Timeline and media downloads use the plugin's HTTP(S) proxy.
  Media stays restricted to three Twitter HTTPS origins with redirect, MIME, byte, pixel, and frame limits.
  GraphQL authentication headers and cookies stay outside media requests.
- `/tw_fetch` distinguishes no new images from failure of every media download.
  Complete failure gives proxy guidance. An empty cache after the fetch gives an explicit warning.
- Removed the fixed 100-image cap from the first full fetch. The first fetch bypasses incremental early termination.
  It continues to timeline end or `max_pages`. Success writes a small completion marker bound to `user_id`.
- Later fetches stop after two consecutive pages without additions.
  An interrupted first task, damaged state, media failure, or account change causes a new full backfill.
- Increased the image cache to 2 GiB for account history and later additions.
  File-count and retention limits remain 5000 entries and 90 days.
- With an empty cache, `/twimg` directs administrators to `/tw_fetch` and its completion notice. Twitter version became `2.0.3`.

### Pendo Web phone layout

- Account details keep a compact horizontal layout on narrow screens. Category labels align near account information.
  Summary and account text truncate inside separate regions.
- Diary calendars and panels have explicit shrink boundaries. Phone screens keep seven columns with compact content markers.
- Detail-dialog actions use two responsive columns on phones. Four actions form a `2 × 2` layout with bottom safe-area space.
- An open dialog hides the back-to-top button and removes it from the interaction layer.
  Close, manage, edit, and delete actions remain accessible.

### Bot Core reload completion notices

- `/reload` keeps its nonblocking two-stage process.
  Configuration publication and background startup produce an immediate acknowledgment.
  Plugin completion or interruption produces one final result and elapsed time in the original administrator chat.
- The Core reload task returns an explicit success state.
  Exceptions, quarantine interruption, and ordinary failures use the failure notice.
  Notification failure only enters the log and leaves the reload result unchanged.
- Repeated `/reload` calls that share one background task register one completion notice.
  Background messages bypass the original event collector. Bot Core version became `0.5.0`.

### QingSSH image help and batch delivery

- Global help, `/ssh help`, the `/ssh` introduction, and connected-session help show `showimg <path-or-pattern> [--page N]`.
  Help states that this command runs inside an SSH connection session.
- `showimg` accepts `./`, relative or absolute directories, and `*`, `?`, and `[]` in the last filename component.
  Results use filename order with five images per page. Previous and next commands cover the complete result set.
- Each image includes its global position, total count, and remote filename. The per-image limit remains 10 MiB.
  Missing arguments, invalid pages, directory wildcards, partial download failure, and send errors have explicit response or cleanup paths.
  QingSSH version became `2.1.0`.

### Test organization and maintenance gates

- Reorganized 314 modules into `core`, `transport`, `scripts`, `tooling`, 29 plugin areas, and cross-plugin contracts.
  Plugin tests, fixtures, and helpers use domain directories. Stable root constants resolve test paths.
- Removed custom asyncio fallback, inaccurate compatibility tests, Pendo historical-script assertions, and the old aiohttp adapter.
  Shared Codex substitutes, HTTP doubles, Pendo database lifecycle, and monitor/reply-checker helpers moved to `tests/helpers/`.
- Renamed historical review, fixes, and regressions groups by actual behavior.
  Split ten test modules that exceeded 1200 lines. The quality gate set a 1200-line maximum.
- The quality gate rejected flat root tests, tests in plugin roots, deep root-path inference, historical group names, and unregistered platform skips.
  CI, command scenarios, and README test paths use the new structure.

### Windows stop entry point

- Added `scripts/stop-bot.vbs` for a double-click stop with a completion result.
  A subsequent double-click on `scripts/run-bot.vbs` starts new processes.
- `scripts/run-bot-monitor.ps1 -Stop` reuses the repository mutex and PID state.
  It first stops the automatic restart monitor.
  It then identifies and stops the Bot log pump, `main.py`, NapCat log pump, and selected NapCat tree by absolute command path.
- Matching is restricted to the current repository and configured NapCat path. Repeated stops are valid.
  Isolated Windows process-tree tests confirmed that unrelated Python processes continued.
- CIM/WMI command-line identity reads have three bounded retries.
  Persistent read failure with an unelevated desktop token requests one UAC elevation.
  The elevated stop process validates every path and process identity again.
- An internal elevation marker prevents repeated UAC requests.
  Canceled elevation, failed elevated startup, or unreadable elevated identity fails closed.
  Verified command-path identity is always necessary before process termination.
- `scripts/sync_to_remote.sh` includes the stop entry point in critical-file integrity and SHA-256 checks.

### Reader documentation and phone layout

- Updated 47 release Markdown files for new users, operators, and maintainers.
  The root README gives one initial route. Topic manuals divide overview, startup, architecture, development, API, configuration, advanced operation, message flow, and plugins.
- The 29 plugin READMEs describe requirements, commands, settings, data limits, fault isolation, and verification.
  Pendo and XiaoQing Chat architecture documents describe internal services and data flow.
- The development guide covers all manifest fields, command inheritance and conflicts, entry order, Context, controlled HTTP, external content, AI, persistence, and services.
  It also covers capabilities, errors, and sensitive auditing. Python-example syntax and runtime-schema contract tests passed.
- Configuration documentation covers every released Core example field, plugin namespaces, secrets, value ranges, admission capacity, AI fallback, and reload/restart requirements.
  Added optional `onebot_token` to `secrets.json.example`. Link anchors match current plugin READMEs.
- The chat acceptance guide covers automation, OneBot, commands, segments, group participation, models, media, memory, security, concurrency, and shutdown.
  It supplies one report template.
- Level-one and level-two headings use consistent topic icons and separators.
  Content and links remain complete. All 47 decoration-normalized content fingerprints matched.
  The structure audit passed 482 headings and 435 separators.
- Markdown ledgers, code-review notes, and remote-sync records became Git-ignored local maintenance material.
  The release index keeps published documentation only.

### Repository cleanup and internal services

- Reviewed Core, 29 plugins, scripts, and test support.
  Call-chain checks covered dead interfaces, empty lifecycle entries, test-only production APIs, dynamic entries, and public contracts.
- Reduced `core/app.py` from approximately 2025 lines to 343.
  `app_config_apply.py`, `app_plugin_context.py`, and `app_lifecycle.py` handle configuration, capability issuance, and lifecycle.
  `XiaoQingApp` remains a stable process facade with the existing startup method.
- Pendo keeps one `Database`, one `pendo.db`, and shared transaction semantics.
  `db_schema.py`, `db_auth.py`, and `db_reminders.py` handle schema, Web authentication, and reminders.
  `event_editing.py` and `event_views.py` handle edit parsing and details.
  Removed superseded metadata parsers, generic JWT code, test wrappers, and Help-order constants.
- Removed the Heartflow synchronization layer and `http_session` forwarding from XiaoQing Chat.
  Unified JSON-root scans, dynamic action results, and delivery commits.
  Local storage bounds malformed values by field. AI, media, background tasks, and lifecycle use explicit fallback boundaries.
- Removed all 45 file-level Mypy exclusions. `python -m mypy core plugins` examined 369 production modules.
  Aligned 48 files with format drift. Ruff formatting and lint passed for 707 Python files.
- Added a release-resource gate that compares Git-tracked plugin resources with setuptools package-data expansion.
  Wheel and sdist contain 67 plugin resources. Training, experiments, and local Minecraft configuration remain external assets.
  `sync_to_remote.sh` checks arXiv models and their SHA-256 digests.
- Serial cleanup validation gave 5959 passed and 10 skipped. Parallel `pytest -n 2` gave 5973 passed and two skipped, with 5975 collected.
  Ruff, Mypy, compileall, strict parsing and duplicate-key checks for 38 JSON files, and 26 JavaScript checks passed.
  Bash, PowerShell, VBS, `pip check`, actual package checks, and `git diff --check` passed. Cache cleanup completed.

### Isolated full UAT and release validation

- During isolated UAT, `scripts/run_full_uat.py` enables Inbound Server and disables outbound WebSocket and plugin watching.
  It restores the exact original `config.json` bytes afterward. Production settings can therefore start a local full UAT.
- Actual `python main.py` startup loaded 29 plugins. Pendo Web listened and shut down cleanly.
  WebSocket and HTTP matrices, Core load tests, compileall, Ruff, Mypy, pytest, and both diff gates passed.
- Final UAT gave 5978 passed, two skipped, and 80.69% coverage.
  Configuration and secret hashes matched. Ports 12000 and 12001 were released.
  Isolated data, locks, and child processes were cleaned up.
  A pre-release run with GitHub Actions arguments also gave 5978 passed and two skipped, with 80.68% coverage.
- The CI format gate covers Python examples in Markdown.
  Windows smoke registers POSIX process-group tests as exact platform expectations. Skip checks compare node, reason, and platform.
  Vector-search candidate indexes use explicit NumPy `int64` arrays for compatibility with fresh type definitions.
- Optional Torch training tests register dependency conditions by node. The base installation remains small.
  Release-resource checks use NUL-separated Git paths with path escaping disabled.
  Chinese filenames therefore have the same actual path meaning on Windows and Linux.

### Production code and arXiv model synchronization

- `scripts/sync_to_remote.sh` treats `plugins/arxiv_filter/best_model/` as a production resource.
  It examines model configuration, weights, tokenizer, and training settings before transfer and verifies integrity afterward.
- Git-ignored `scripts/sync_to_remote.local.sh` contains the remote host and destination directory. Operators edit their local settings directly.
- Synchronization uses `.gitignore` for local caches and training products.
  Rsync exclusions protect production settings, Minecraft connections, logs, databases, plugin caches, backups, and exports.
- Added checksum comparison, delayed replacement, and partial-transfer retention.
  Regular files pass validation before transfer. Critical code, startup files, and arXiv weights receive per-file SHA-256 checks afterward.
- The default is a dry run. `--apply --confirm-delete` is necessary for actual writes.
  Operators control service stop and startup. Script help gives the preview, apply, and verification sequence.

### Layered help for phone screens

- `/help` uses progressive detail. Overview pages list plugins. Plugin pages list top-level entries. Command-path pages list direct children.
  Leaf pages show usage, aliases, permissions, chat scopes, and examples. All plugins share the format.
- Menus and search results use approximately 34 display columns on phones.
  Wrapping occurs at argument boundaries. Navigation includes more details, previous and next pages, and a return entry.
  Stable codes, permissions, and examples appear in leaf details.
- `/help json [query] [page N]` keeps all fields, a flat catalog, and pagination. The `bot_core` version became `0.4.0`.

### Minecraft message help

- `/mc help` shows `/mc say <message>` for broadcasts and `/mc tell <player> <message>` for private game messages.
  With log monitoring enabled, player chat, joins, and departures go to the QQ private chat that started the connection.
- Examples use `/mc say 大家好`. At this release, `say`, `tell`, and `tellraw` passed unchanged to RCON.
- Manifest help includes broadcast and private-message examples. Minecraft version became `4.0.1`.

### arXiv source dates and Codex summary identity

- `/arxiv` replies show the actual source-list publication date. Inference caches use separate source dates.
  An invalid source date returns the paper list and omits Codex delivery.
- Codex identifies historical summaries and active tasks by source-list date plus normalized paper-link set.
  Different same-day contents create a new task. Link order, versions, PDF suffixes, and query parameters normalize to one set.
- Added tests for cache-generation changes, multiple same-day lists, active tasks, and repeated normalized sets.
  `arxiv_filter` became `0.2.0`. `codex` became `1.1.1`.
- The Linux parallel release gate publishes PID markers through same-directory temporary files and atomic replacement.
  Monitor-cleanup tests read complete snapshots.

### Shell Git Bash terminal and Windows errors

- Added public `config.plugins.shell.terminal`. The default cross-platform terminal is `direct`.
  Windows can select `git-bash` with an explicit `executable`.
  Git Bash starts with `--noprofile --norc -c`. Validation uses explicit paths and fails closed.
- Commands pass dangerous-pattern and first-entry enablement checks before Git Bash execution.
  `/shell list` examines entries for the selected terminal. `/shell help` shows matching examples. Shell version became `2.1.0`.
- Windows production `/shell ls` and `/shell pwd` support Git Bash. Program-path resolution errors produce clear user messages.
- `/shell list` separates entries available through the Bot PATH from missing entries.
  The enabled list controls authorization. Deployment manages tool installation.
- Help uses platform and terminal-specific examples.
  Windows direct uses `cmd /c dir` and `cmd /c cd`. Git Bash, Linux, and macOS use `ls` and `pwd`.
  Operators prepare the environment and terminal path.

### Windows production startup

- Repaired script-directory resolution when Windows PowerShell 5.1 starts `scripts/run-bot-monitor.ps1` through `-File` or `run-bot.vbs`.
  Relative defaults resolve after parameter binding. Directory validation fails closed.
- Added source contracts and actual Windows PowerShell tests for default startup without `-BotRoot` and configured Bot environment scope.
  All 31 monitor tests passed.
- Default-path tests use isolated settings. Windows smoke uses two pytest workers and sufficient CI time for PowerShell startup.
- The monitor organizes deployment settings, path resolution, process identity, startup, and monitoring in order.
  Operators prepare Conda, virtual environments, and Python paths. The script calls `python` from `PATH`.
- `config/config.json` supplies `napcat_account`. The launcher passes it as the first positional NapCat argument.
- Optional `mkl_threading_layer` sets `MKL_THREADING_LAYER` during creation of the Bot log-pump tree.
  The parent environment is restored immediately afterward. The setting coordinates MKL/OpenMP runtimes.
- Damaged PID contents, ended processes, and changed command-line identities cause PID-file cleanup.
  CIM/WMI errors fail closed. The monitor uses a UTF-8 BOM for Windows PowerShell 5.1.
  Parsing regression tests cover both PowerShell hosts.

### Pendo Web login credentials

- `/pendo web token` sends the original login Code privately. The Code is valid for one redemption within seven days.
- Browser Cookie sessions last seven days. Codes and browser sessions persist in the existing `pendo.db` with digest records.
  Absolute expiry remains valid after Bot or Pendo Web restart.
- `/pendo web widget-token` defaults to 365 days. Tokens have persistent per-user records.
  `/pendo web widget-revoke` revokes them explicitly.
- Code, browser-session, and Widget JWT lifetimes use seconds and the same absolute-expiry calculation.
  Browser Cookie/CSRF and read-only Widget Bearer permissions remain separate.
- Updated login pages, command help, settings, and Scriptable documentation.
  Added tests for original Code delivery, lifetime arguments, and configured NapCat startup arguments.
- Full `pytest -q -n 2` gave 5941 passed, five skipped, and 80.76% coverage.
  Repository Ruff, Mypy for 307 production files, compileall, and `git diff --check` passed.

## 4.2.0 — 2026-08-05

### v4.2.0 release

- Released `v4.2.0`. The release includes complete code-review corrections, runtime service boundaries, safe defaults, unified AI/VLM routing, and command UAT for all plugins. It also includes Pendo and XiaoQing Chat improvements, and updated release and operations tools.
- Release acceptance used existing production `plugins/*/data/` and log snapshots. Pendo and QingPet WAL databases passed `quick_check` and complete `integrity_check` before layout migration. All 29 plugins completed data-layout upgrades in an isolated data root. Three consecutive runs completed normal startup, graceful shutdown, and port release.
- Complete UAT passed the WebSocket/HTTP command matrix, dynamic CRUD/cleanup scenarios, Core load tests, compileall, Ruff, mypy, pytest, and both diff checks. The full suite recorded 5914 passed, 1 skipped, and 80.79% coverage. Configuration bytes were restored, and secrets hashes matched. External dependencies and real paid-model quality tests used explicit opt-in stages.
- Completed optional-feature dependency contracts identified in clean environments. Full source-checkout dependencies gained pandas and scikit-learn for arXiv data preparation and training. The `astro` extra and Astro Tools Manifest gained SciPy for cosmological integration. Shell Windows path normalization was fixed. Windows monitor test probes switched to the cross-platform temporary-directory API.

### All-plugin WebSocket command checks, Pendo examples, and documentation

- Added `bash scripts/run_full_uat.sh` as the unified deployment acceptance entry point. It combines isolated plugin data, real startup/graceful shutdown, HTTP/WS command matrices, dynamic business scenarios, Core load tests, and CI checks. Reports support later inspection. Explicit selection is necessary for external dependencies and paid-chat quality tests. Plugins, commands, and case types support targeted reruns.
- UAT and the Windows monitor use `python` from the current `PATH`. The deployment environment manages Conda, virtual environments, interpreter paths, and dependencies.
- UAT text subprocesses explicitly use UTF-8 and `errors="replace"`. Direct Git Bash execution of `pytest -n 2` passed cross-platform encoding checks.
- Tested Core and all 28 command plugins through Inbound WebSocket `/ws`. Tests exercised 370 command nodes, 404 `invalid_examples`, and all `examples`. External-service connection errors and expired credentials followed plugin fallback paths.
- Standardized Pendo quick-token examples on `plan:2026-08-01` and `deadline:2026-08-01T18:00`. These match the parser contracts `plan:YYYY-MM-DD` and `deadline:YYYY-MM-DDTHH:MM`.
- Standardized Dispatcher documentation on linear stages A–G: gates → URL → bot-name-only message → Session → command → unknown command → smalltalk fallback. URL parsing follows processing gates and mute checks. The observation entry receives ordinary-chat candidate messages.
- README and the Core module table use actual component names. Coverage includes `ai`, `plugin_execution`, `delivery`, `safe_http`, `bounded_http`, `atomic_store`, `bounded_file_cache`, `durable_fanout`, and `async_keyed_lock`. It also includes `auth`, `inbound_policy`, `lifecycle`, `public_errors`, `sensitive_audit`, and `version`.
- `docs/03-plugin-development.md` supplies a five-step plugin tutorial. It covers subcommands, `command_invocation`, `data_dir`, loading, and testing. README links to the tutorial.
- Historical validation: Git Bash `pytest -n 2` collected 5915 items and exited with code 0: 5914 passed and 1 skipped. UAT runner and encoding regressions recorded 8 passed. Ruff and `git diff --check` passed. Corrected `plan:`/`deadline:` tokens and actual WS command paths were exercised.

### Unified AI/VLM model registry and routing

- Added `core/ai.py` as a controlled path for OpenAI API-compatible services. Public connection settings use `config.ai.providers`. Model profiles and modalities use `config.ai.models`. Plugins declare ordered model chains and request limits in `config.plugins.<plugin>.ai.routes`. API keys use `secrets.ai.providers`.
- `XiaoQingApp` creates a narrow `AIService` bound to each plugin name. Callers supply route, messages, and task parameters. Core manages provider keys and configuration namespaces. Each `complete()`/`list_models()` reads the latest configuration snapshot.
- The first route profile is the primary model. Recoverable errors use ordered fallback. Authentication and argument errors return directly. `total_timeout_seconds` limits the complete chain. Administrators can select a profile with `pinned_model`. `AICompletionResult` returns content, routing, and attempt information.
- Updated the unified AI/VLM registry descriptions in `docs/04/06`.

### Runtime version resolution

- Added `core/version.py` to resolve runtime `VERSION` from `pyproject.toml` or installed wheel metadata. The recorded version was `4.2.0`. `/health`, Inbound Server, and other consumers use this value.

### Crash-safe notification fanout, delivery receipts, and file-cache limits

- Added `core/durable_fanout.py`. It records crash-safe progress per scheduled-notification target. Delivery resumes from persistent checkpoints after a process restart.
- Added `core/delivery.py` with process-local delivery receipts for commit-after-ack state updates. Failed HTTP responses or WS Action writes roll back attached receipts.
- Added `core/bounded_file_cache.py`. Plugins share its crash-safe disk cache with TTL, LRU, entry limits, and byte limits.

### Packaging, CI, and repository line endings

- Added `.gitattributes` line-ending policy. Repository text uses LF for storage and checkout: `* text=auto`, with `eol=lf` for `.py/.toml/.json/.yml/.md` and similar files. Windows `cmd/vbs` files use CRLF. Line-ending normalization caused the large diff in that update.
- Simplified `Dockerfile` and `.dockerignore`. Added `MANIFEST.in` for package contents and `.github/dependabot.yml`. Simplified the `tests.yml` workflow.
- Removed the `dependency-locks` workflow and `requirements/` lockfile directory. Root `requirements.txt` maintains direct dependencies. pip resolves them for the current Python version and platform.

### Additional cleanup outside code-review corrections

- Removed code-review process files, coverage artifacts, runtime path lists, and replaced release scripts.
- Removed plugin process documents and replaced AI clients. Pendo AI calls use `core/ai.py`. Typed coordinators handle XiaoQing Chat PFC stages.

### Complete code-review corrections

- Completed 285 code-review corrections. These aligned privileged access, network/output limits, transactions/idempotency, privacy isolation, cancellation cleanup, release artifacts, and test-reliability checks.
- Removed unused Sony sign-in code, unused ADNMB modules, replaced QingPet APIs, the manual pytest runner, and tracked `coverage.json`. `.gitignore` manages generated coverage files.
- Split Xiaoqing Chat reply generation, PFC, and the Codex runner into typed stage coordinators. AST regression checks limit renewed code growth.
- QingPet group statistics use a constant number of aggregate queries. `users.coins` is the authoritative balance source. Added discrepancy detection from persistent checkpoints and net `asset_ledger` changes.
- Updated plugin development/API/advanced-use documentation, Voice/Signin examples, Pendo dependency descriptions, and actual GitHub project links.

### Remote synchronization script delivery

- Added `scripts/sync_to_remote.sh`, fixed-root sentinels, operations instructions, and security-contract tests to versioned project assets. A clean checkout supports synchronization previews.
- Shell scripts use LF. The Git index keeps the executable bit on `scripts/sync_to_remote.sh`. The default mode is dry-run.

### Pendo Web details and longer token validity

- Set Pendo `WEB_TOKEN_EXPIRE_HOURS` to 168 hours, or one week.
- Standardized Pendo Web date inputs, including `notes.js`, `tasks.js`, and `transfer.js`, on `YYYY-MM-DD` text fields. Native calendar popups were removed. Inputs match ledger quick entry and support direct keyboard input.
- The `ledger.js` custom-period date fields use a separate full-width row. Both fields remain fully visible.
- Standardized date-field styles. Removed default bold digits and focused-field background fill for consistent pages.

### Lightweight Pendo task creation

- Simplified empty `/pendo todo add` interaction. It asks for content and planned date, then creates the task after date confirmation. Deadline, reminder, category, priority, and tags use single-line quick parameters. The creation receipt focuses on user-supplied fields.
- `/pendo todo add <content> ...` supports `plan:`, `deadline:`, `remind:`, `cat:`, `p:`, and `#标签`. Later session steps handle interactive extended fields.
- Updated Pendo README, chat help, and plugin documentation to explain default interaction and advanced parameters.
- Historical validation ran `python -m pytest tests/plugins/test_pendo.py -q` and `python -m pytest tests/plugins/test_pendo_review_regressions.py tests/plugins/test_pendo_fixes.py -q`. It also ran `python -m compileall plugins/pendo/main.py plugins/pendo/handlers/task.py -q` and `git diff --check`.

### Automatic arXiv summaries

- `/arxiv` and weekday schedules first send paper lists. They then submit all positive paper links to the Codex `astro-ph` session for background Markdown summaries. List delivery and summary tasks have separate message paths and error handling.
- The Codex `astro-ph` session is created on demand. First use submits a silent initialization task, then the day's links. Successful same-day results can be sent again. Other states create summary tasks.
- Summary prompts instruct Codex to read `arxiv-summary-methodology.md` in the working directory. The workspace file maintains the methodology text.
- `astro-ph` is protected by default. `/codex delete astro-ph --force --protected` is necessary for deletion. History is archived in `plugins/codex/data/deleted_sessions/`.
- Moved arXiv summary logic into `plugins/codex/arxiv_summary.py` and `plugins/arxiv_filter/codex_summary.py`. The main Codex queue keeps generic metadata, queueing, archive, and send functions.
- Updated root README and `docs/00-09` descriptions for `arxiv_filter` and `codex` configuration, message flow, runtime data, and plugin manuals.
- Historical validation ran `python -m pytest tests/plugins/test_arxiv_filter.py tests/plugins/test_codex.py -q`, `git diff --check`, and trailing-whitespace checks on added/changed files.

## 2026-05-24

### Pendo private continuation for group-chat sessions

- Pendo privacy mode creates group multi-turn command prompts and Sessions in private scope, with the source `group_id` recorded.
- Pendo reply-scope helpers select private or group Sessions from privacy settings. Subsequent messages continue in that scope.
- Applied the helpers to interactive task creation, ledger entry, diary templates, and event detail/conflict-confirmation Sessions. Updated Pendo README group-privacy instructions.
- Added Session regressions for private continuation from groups in privacy mode. Tests also cover continued group Sessions when privacy mode is disabled.
- Historical validation ran `python -m pytest tests/plugins/test_pendo.py -q` and `git diff --check`. It also ran `python -m compileall plugins/pendo/main.py plugins/pendo/utils/session_utils.py plugins/pendo/handlers/task.py plugins/pendo/handlers/ledger.py plugins/pendo/handlers/diary.py plugins/pendo/handlers/event.py`.

## 2026-05-18

### Interactive Pendo task creation

- Empty `/pendo todo add` starts a multi-turn flow. It collects content, planned date, deadline, reminder time, category, priority, and tags in order.
- Kept single-line `/pendo todo add <content> ...` with `plan:`, `deadline:`, `remind:`, `cat:`, `p:`, and `#标签` parameters.
- Extended fields accept `0` for defaults: default planned date, open deadline, disabled reminder, default category, priority 3, and empty tags.
- Added todo regressions for empty-argument Sessions, quick entry, defaults, and planned-date validation messages.
- Historical validation ran `python -m pytest tests/plugins/test_pendo.py -q` and `python -m pytest @files -q`, where `files` contains `tests/plugins/test_pendo*.py`. It also ran `python -m compileall plugins/pendo -q` and `git diff --check`.

## 2026-05-15

### Pendo Web ledger filter styles

- Standardized lower/upper amount fields with period, account, and category filters. Shared properties include height, padding, border, radius, background, and font weight.
- Amount filter hover/focus states follow the adjacent custom-select rules. Quick-entry amounts and custom-date fields keep their own styles.
- Historical validation ran `node --check plugins/pendo/web/static/js/pages/ledger.js` and `git diff --check`. The Pendo Web demo supplied computed-style checks.

## 2026-05-12

### Codex image result delivery

- Changed `plugins/codex` runtime layout. Each label's conversation records, image copies, and task artifacts use `plugins/codex/data/session/<label>/`.
- Each Codex task receives image-output instructions. Generated images go to that task's artifacts directory, with paths in the final reply.
- Completion parses final text and the artifacts directory. A fallback scan examines images added to `$CODEX_HOME/generated_images/` during the task. Local images are copied to the session image directory and returned with text through QQ image segments. Long text is divided before images are sent.

## 2026-05-10

### Codex background-task plugin

- Added `plugins/codex`. `/codex create <name> [cwd:<path>]` creates a session label. `/codex <name> <task>` submits a task to that session.
- Codex tasks use a separate plugin queue. Framework Sessions handle chat interaction. Tasks are sequential within each label. Labels run concurrently up to `max_parallel_jobs`. Completion sends a `[codex:<label> #<job_id>]` result.
- Added `/codex list`, `/codex status`, `/codex cancel`/`stop`, `/codex clear`, and `/codex delete`. Session creation can specify a working directory.
- Added path normalization and allowed-directory validation. The historical default directory was `C:/Users/testuser/Desktop/XiaoQing/XiaoQing_Codex`. QQ paths can use `/` separators.
- Session indexes and per-label conversation JSONL files use `plugins/codex/data/`. `.gitignore` continues to exclude runtime data.

### Shell path parsing

- Improved argument splitting and path detection in `plugins/shell`. Arguments keep Windows backslashes.
- QQ input can use `/` separators. The plugin normalizes paths for the Bot host system. Separate rules classify URLs, `cmd /c`, and `xcopy /Y` arguments.
- Windows built-ins such as `copy`, `del`, and `type` execute through `cmd /c`. External programs such as `cp`, `xcopy`, and `robocopy` execute directly.

### Pendo ledger entry

- Empty `/pendo ledger add` starts interactive entry. Amount and description use text input. Transaction type, account, and category use numbered selections.
- Kept single-line `/pendo ledger add <amount> <description> ...`. Supported parameters include `cat:`, `in/out/transfer`, `account:`, `to:`, `merchant:`, `date:`, and `remark:`.
- Added ledger and main-entry tests for interaction steps, defaults, transfer validation, and quick entry.

### Documentation synchronization

- Updated root README, `docs/00-09`, `docs/README.md`, and the Pendo, Shell, and Codex READMEs. Manuals describe project structure, commands, configuration, background queues, and path rules.
- Added descriptions of `PluginContext.send_action()`, background queues, Codex/Shell configuration, interactive ledger entry, and plugin statistics.
- Reviewed README and `docs/*.md` wording as project descriptions and user manuals. This CHANGELOG holds update history.

### Validation

- Historical validation ran `git diff --check`.
- The maintainer's local Python environment ran `python -m pytest tests/plugins/test_codex.py tests/plugins/test_shell_plugin.py tests/plugins/test_pendo.py -q`. The result was `180 passed`.

## 2026-05-08

### Pendo Web ports

- Changed the default Pendo Web port from `8765` to `12001`. This avoids `WinError 10013` binding failures from Windows TCP excluded port ranges.
- Standardized local port documentation. OneBot/NapCat use `11000`/`11001`. XiaoQing inbound uses `12000`. Pendo Web uses `12001`. Alternative examples use `12002`/`12003`.
- Updated default addresses in README, Pendo/configuration documentation, Scriptable widget instructions, nginx reverse-proxy examples, and Pendo test instructions.
- Historical production Windows checks confirmed binding on `127.0.0.1:12001` and `127.0.0.1:12003`. Validation ran `pytest tests/test_config.py tests/test_server.py tests/plugins/test_pendo.py tests/plugins/test_pendo_web_widget.py` for configuration, inbound server, and Pendo regressions.

## 2026-05-07

### More natural xiaoqing_chat conversations

- Added `humanize` settings. Typing delay uses input reading size and output length. Multi-part replies use configurable intervals.
- Changed personality-state refresh. Current mood remains stable during its validity period and is sampled again after long idle periods. This reduces tone changes on every turn.
- Tightened tone rules in prompts and the reply checker. This reduces repeated filler openings such as “哈哈”, “笑死”, and “啊这”. Unfamiliar topics first express confidence levels or request clarification. Generation uses complete meaning as its basis.
- Meme injection puts frequently learned expressions into context after they reach the configured threshold. Less frequent expressions enter review.
- Rewrote media-analysis prompts to start with objective image/meme descriptions. Added optional meme-background extraction, caching, and context markers. These reduce forced meme references based only on image text.
- Added reply-checker, expression-injection, typing-delay, and mood-lifecycle tests. Historical validation ran `python -m compileall -q plugins/xiaoqing_chat`, `git diff --check`, and `python -m pytest tests/plugins -k "xiaoqing or reply_checker" -q`.

## 2026-05-03

### Pendo Web task and ledger UI corrections

- Removed the six-task return limit from the “today and overdue” overview. All tasks for today and earlier now appear.
- Changed task-list presentation to flatter sections and rows, with fewer nested rounded containers.
- Improved ledger quick-entry and filter forms. Field grids, labels, and amount-filter widths are consistent.
- Historical validation ran `pytest tests/plugins/test_pendo_web_items.py tests/plugins/test_pendo_web_tasks.py tests/plugins/test_pendo_web_widget.py -q` for related Web regressions.

## 4.1.0 — 2026-05-01

### Documentation structure

- Rewrote root `README.md` to describe XiaoQingBot as a QQ bot for continuous operation. Added capabilities, code structure, message flow, deployment, plugins, tests, and troubleshooting links.
- Rewrote `docs/README.md` with the `docs/00-09` reading order, intended readers, plugin documentation links, and maintenance rules.
- Updated `docs/00-overview.md` through `docs/09-plugins.md` as manuals for the project state at that release. Coverage includes purpose, quick start, architecture, development, Core modules, APIs, configuration, advanced use, message flow, and built-in plugins.
- Revised documentation tone to reduce chronological narration, questions, exaggeration, and rigid wording. Technical accuracy remains a requirement, with more natural reading.
- Updated `docs/pendo-scriptable-widget.md` with the Pendo iPhone Scriptable widget's read-only API, configuration, and daily uses.

### Pendo documentation and test material

- Kept plugin-level `README.md` and `ARCHITECTURE.md` entry points in `plugins/pendo`. README covers daily use and deployment. ARCHITECTURE explains data models, commands, Web APIs, scheduling, and import/export for maintainers.
- Removed the largely duplicate `plugins/pendo/Pendo个人时间与信息管理中枢.md`. `plugins/pendo/README.md` contains the user instructions.
- Standardized official Pendo validation on repository automated tests. Git ignore rules manage local black-box instructions and execution records.
- Cleaned Pendo test reports. Kept final reports, key screenshots, migration samples, complete test scripts, and useful retrospective reports. Removed intermediate and one-shot results from version control.

### xiaoqing_chat documentation and test material

- Added plugin-level `README.md` and `ARCHITECTURE.md` in `plugins/xiaoqing_chat`. README covers humanlike chat, multimodal input/output, attention gates, rate limits, memory, planner, reply checker, memes, and test commands. ARCHITECTURE covers the main flow, context construction, should_reply, planner, generation, multimodal delivery, memory, and experiments.
- Revised `plugins/xiaoqing_chat/xiaoqing_chat测试.md` into a complete test prompt. Tests first read or create `CURRENT_RUN_ID`, reconstruct progress from `xiaoqing-test-results.jsonl`, and continue through the case_id matrix.
- Added the latest humanlike-conversation test framework to that prompt. Coverage includes large-group chat, text, QQ face, NapCat mface, images, memes, reply references, coreference, rate limits, planner, checker, and multi-turn conversation.
- Kept useful reports, including 3000-turn conversation information, final analysis, and key data. Removed intermediate state files to reduce repository noise.

### xiaoqing_chat reply decisions

- Improved the should_reply path. The attention gate handles `@`, bot names, reply-to-bot, contextual coreference, and group interjections together.
- Coreference detection covers recent context, Bot-related pronouns, and explicit mentions. This improves recall for references to “小青”.
- Removed duplicate weights outside the main decision path, including `heartflow.weight_mentioned`. Clarified planner, frequency-control, and attention-gate responsibilities.
- Changed rate-limit design to separate base cooldown, random group interjection, strong triggers, and contextual triggers. Strong triggers enter their applicable reply path directly.
- Added unit and humanlike-experiment tests for attention gates, rate limits, multimodal reception, media replies, and contextual triggers.

### Repository maintenance

- Updated `.gitignore` for plugin experiment output, test intermediates, runtime logs, cache files, and local state.
- Reviewed archive value in `plugins/xiaoqing_chat/experiments`. Kept experiment code and reproducible scripts. Bulk output and temporary data use ignore rules.
- Added root `CHANGELOG.md` as the project update record. Later entries record dates, changes, affected scope, and validation results.

### Simpler Pendo ledger entry

- Simplified ledger addition to reduce the necessary arguments for daily entry.
- Updated the ledger handler, main entry point, and documentation. Chat behavior and plugin instructions describe the same flow.
- Expanded `tests/plugins/test_pendo.py` with common argument combinations and ledger regressions.

### Pendo documentation refresh

- Refreshed Pendo descriptions in its README/architecture guide, project README, docs directory, and plugin catalog.
- Organized data-model, Web console, Scriptable widget, migration, backup, and troubleshooting information.
- Reduced repeated content and concentrated plugin instructions in README and ARCHITECTURE.

### Pendo timezone-aware reminder parsing

- Reminder parsing accepts timezone-aware times.
- `plugins/pendo/utils/validators.py` handles existing timezone-aware reminders consistently. Migration and loading use explicit time rules.
- `tests/plugins/test_pendo_event_migration.py` covers timezone-aware reminder upgrades.

### xiaoqing_chat test specification

- Reworked `plugins/xiaoqing_chat/xiaoqing_chat测试.md` into a complete reusable test procedure.
- Runs recover through `CURRENT_RUN_ID`, state files, and the case_id matrix. Compacted task context can resume from item-level progress.
- Added requirements for multimodality, humanlike behavior, group interjections, planner, and reply checker.

### Versioned plugin test material

- Added Pendo and xiaoqing_chat test prompts to the repository as plugin test entry points.
- Updated `.gitignore` to exclude bulk intermediate report files.
- Removed large one-shot historical output with little reuse value. Useful final reports and reproducible scripts remain.

### Expanded Pendo redesign coverage

- Expanded command, Web, migration, and regression tests for the Pendo redesign.
- Added coverage for dashboard, settings, transfer, and other Web surfaces.
- Fixed regressions in scheduled commands, transfer bundles, settings API, and dashboard overview.

## 2026-04-30

### Pendo redesign flow protection

- Strengthened core flows for events, tasks, notes, diaries, ledger, search, settings, import/export, and Web APIs.
- Updated command routers, handlers, services, models, Web APIs, and frontend pages. Chat commands and the Web console use the same redesigned data model.
- Added multi-agent test reports, black-box scripts, browser screenshots, migration samples, and pytest coverage.
- Windows PowerShell test commands first enumerate pytest files, then pass explicit paths to pytest.

### Pendo test-output ignore rules

- Updated `.gitignore` to exclude intermediate redesign reports, browser output, and generated data from ordinary version control.
- Kept final reports, scripts, and key screenshots. Git ignore rules manage one-shot test files.

## 2026-04-29

### Pendo event references and child validation

- Fixed lost reference information during Pendo note-reference edits.
- Added validation for event collection children. Event trees accept valid child nodes only.
- Synchronized master fixes during the Pendo redesign branch merge.

### xiaoqing_chat multimodal replies

- Improved media-reply processing across the main LLM, prompt builder, reply checker, and marker resolver. Images, memes, and QQ face markers have more consistent handling.
- Moved media resources into the data directory to separate plugin code and runtime data.
- Kept visible text when an emoji arrives. The LLM and reply checker can use the emoji context.
- Corrected documented media-library paths to reduce deployment ambiguity.

## 2026-04-28

### Simpler xiaoqing_chat main flow

- Refactored reply processing. Unified stage coordinators handle media replies and planner logic.
- Added one media marker resolver for image, meme, QQ face, and mface send markers.
- Updated frequency control, context builder, reply generator, reply checker, memory retrieval, and runtime state to clarify the main flow.
- Updated README, configuration documentation, and advanced-use instructions with new settings and multimodal delivery.
- Added runtime-protection, review-regression, prompt-builder, media, and reply-checker tests.

## 2026-04-25

### Pendo note workflow

- Improved note creation, references, updates, and display.
- Added explicit labels to action buttons in individual event details.
- Aligned event fixtures with the current data model.

### Core and LLM compatibility

- Fixed inbound-manager status-provider binding so status queries use the correct provider.
- Passed vision LLM extra payload through to multimodal model calls.
- Fixed planner and checker edge cases to reduce incorrect decisions in those stages.

## 2026-04-24

### Pendo event graph alignment

- The event graph consistently carries event leaf-node paths.
- Aligned Markdown export with the event graph so exports represent relationships in multi-node events.
- Aligned Web surfaces with the event graph. Web APIs and frontend pages use the same event model.

### xiaoqing_chat and Pendo widget protection

- Improved reply flow and model handling to reduce mismatches between multimodal context and model calls.
- Strengthened Scriptable widget data retrieval to reduce the effects of mobile read failures.

## 2026-04-23

### Pendo scheduling and reminders

- Improved scheduled tasks and reminder delivery for more consistent behavior between reminders, events, and background tasks.
- Tightened reminder-state updates and schedule triggers. Explicit state controls deduplication and acknowledgment.

### xiaoqing_chat media flow

- Simplified media processing and clarified responsibilities for inbound media, context construction, and send markers.
- Updated ignore rules. `.gitignore` manages local worktree directories.

## 2026-04-22

### Pendo milestone reminder semantics

- Added milestone briefings for more useful summaries of complex events and staged arrangements.
- Narrowed acknowledgment rules to reduce accidental interpretation of ordinary text as confirmation.

## 2026-04-21

### v4.0.0 release

- Released `v4.0.0` as the new mainline with updated multimodal replies, Pendo Web, and documentation.
- Adjusted user documentation tone so README and plugin instructions describe daily use more directly.

### xiaoqing_chat multimodal reply protection

- Fixed multimodal edge cases to improve stability for mixed image, emoji, and text messages.
- Media segments and OneBot emoji use consistent transport rules. Send markers match message segments.
- Improved reply flow and emoji repair to align the reply checker, media parsing, and final delivery.
- Updated documentation with new media-reply behavior and repair procedures.

### arxiv_filter test compatibility

- Training tests run according to pandas availability. The ordinary test suite remains independent.

## 2026-04-20

### xiaoqing_chat media-reply pipeline refactor

- Improved media replies so text, images, memes, and QQ face can share one reply pipeline.
- Refactored the pipeline. Unified stages and state models replace scattered helpers.
- Established the basis for later marker-resolver, reply-checker, and multimodal-context alignment.

## 2026-04-19

### xiaoqing_chat image conversation and emoji analysis

- Added image-conversation support. Image content contributes to group-chat understanding and reply generation.
- Updated `0.2.0` documentation for image conversation, multimodal settings, and usage.
- Emoji images, animated emoji, and media context enter semantic analysis with structured media descriptions.

### Pendo event editing and reminders

- Improved event editing and reminders to align schedule changes, reminder generation, and display.
- Adjusted watcher defaults and plugin configuration behavior to reduce unexpected file-watching or hot-reload behavior under default settings.

### Audit fixes and documentation

- Fixed audit findings in Core and multiple plugins. Changes cover runtime, plugin settings, media context, and test boundaries.
- Completed review follow-up and remediation items. Restored the review checklist and moved temporary review notes from project documentation to local storage.
- Updated runtime, xiaoqing_chat, and Pendo documentation to match implemented interfaces.

## 2026-04-17

### Pendo widget and note-title parsing

- Refined Scriptable widget display and synchronization.
- Improved note-title parsing for more reliable quick entry and later retrieval.

## 2026-04-15

### Pendo calendar and widget synchronization

- Improved the calendar view for clearer event browsing and time-range display.
- Updated Scriptable synchronization to improve mobile summary updates.
- Refactored widget backgrounds for native iOS dynamic dark-mode switching and transparent decoration.

## 2026-04-14

### Pendo Web widget and service flow

- Improved widget-related APIs and Web server flow.
- Updated widget read paths and server responses for more reliable Scriptable data retrieval.

## 2026-04-12

### Pendo Scriptable widget

- Added Scriptable widget support for Pendo events, tasks, reminders, and summaries on the iPhone home screen.
- Added read-only APIs and a script entry point for mobile access to today's information.

## 2026-04-08

### Pendo Web demo and date ranges

- Improved demo access to simplify startup and validation.
- Improved Web date-range handling to reduce time-boundary errors in dashboards, statistics, and demo data.
- Strengthened demo import paths for consistent imports from different working directories.

## 2026-04-06

### Pendo note parsing and multiline input

- Improved note parsing and help text for clearer creation, content addition, and query commands.
- Multiline note input keeps all line breaks.

## 2026-04-03

### Pendo reminder and ledger interaction

- Improved reminder structure for easier reading and subsequent confirmation or action.
- Changed ledger-entry question order to match daily input habits more closely.

### xiaoqing_chat group participation

- Increased participation in ordinary group-chat interjections.
- Established a higher base participation rate for later attention-gate, frequency-control, and humanlike-behavior tests.

## 2026-04-01

### Pendo Web token input

- Fixed recognition of pasted token messages.
- Users can paste tokens copied from chat or other sources directly. This reduces login failure points.

## 2026-03-31

### Pendo Web details

- Standardized chart ranges and token delivery. Pages share statistical definitions.
- Improved mobile navigation, cards, and forms for small screens.
- Improved search-result readability for mixed note, event, ledger, and task queries.

## 2026-03-30

### Pendo Web filters, statistics, and exports

- Web filters and statistics share filter conditions and range definitions.
- Improved the exporter, diary analysis, and note UI for data export, review, and browsing.
- Note commands accept explicit titles and bodies for reliable structured note creation in chat.
- Aligned FastAPI imports, task display, and related documentation.

### Test and dependency stability

- Fixed pytest stability issues in dict and earthquake plugins.
- Pendo Web authentication and transfer tests run according to PyJWT availability. The base suite remains independent.
- Fixed scheduled-shutdown cancellation and CI dependency issues for reliable test completion and background-task cleanup.

## 2026-03-29

### Pendo Web transfer and analytics

- Expanded transfer support and improved data import, export, and migration.
- Improved analytics for more useful dashboard and statistics summaries.
- Fixed scheduling-shutdown cancellation and CI dependencies to reduce intermittent test failures.

## 2026-03-28

### Pendo command checks and diary mood

- Refined command guards. Argument completeness and format are validated before business processing.
- Improved diary mood UI for clearer entry and display.

## 2026-03-27

### Pendo Web UI V3.0 merge

- Merged the Web console with dashboard, events, tasks, ledger, notes, diary, search, stats, and settings pages.
- Added PyJWT, FastAPI, uvicorn, and other Web UI dependencies.
- Fixed API prefixes and relative paths for nginx subpath deployment. Deployment under `/pendo` works.
- Strengthened event reminders and edits to reduce state conflicts between Web UI and chat operations.

### V3.3.0 documentation refresh

- Updated root README, `docs/00-overview.md`, and `docs/09-plugins.md` with Pendo Web UI capabilities.
- Refreshed all documentation for V3.3.0, covering architecture, plugins, deployment, and usage.
- Removed obsolete docs/plans and docs/superpowers. Consolidated pytest temporary directories in `.pytest_cache`.
- Refreshed README and docs visual style with badges, notice blocks, and clearer entry descriptions.

## 2026-03-26

### Pendo Web UI visual redesign

- Redesigned the Web UI with consistent page layouts, navigation, module colors, and interaction visuals.
- Established the basis for later mobile refinements, filter bars, and component consistency fixes.

## 2026-03-25

### Initial Pendo Web UI implementation

- Added Web configuration and package structure, with FastAPI server, dependency injection, API routers, and static-resource entry points.
- Added JWT authentication and tests as the basis for console login.
- Added authentication, item CRUD, dashboard, search, statistics, settings, and configuration API endpoints.
- Added chat commands to manage and open the Web UI.
- Added the HTML shell, CSS, JS router, API client, store, and application bootstrap.

### Pendo Web pages

- Added dashboard, event calendar, task kanban, ledger quick-add, note-card grid, diary timeline, search, statistics, and settings pages.
- Added shared UI components and a Chart.js loader. Dashboard and statistics reuse chart-loading logic.
- Redesigned select elements and filter bars for clearer, more consistent controls.
- Refactored the ledger page and fixed sorting and pagination.

### Pendo Web fixes and documentation plan

- Fixed API error handling, chart-loader paths, Web help, API response formats, FAB menu, and multiple UI data issues.
- Added the Web UI design specification, review revisions, and a 20-item implementation plan.
- Improved field filtering in list commands. Ledger date-range parsing uses shared time-parsing logic.

## 2026-03-24

### Pendo ledger implementation

- Added the `LedgerItem` model, field constants, database columns, ledger-type icons, and formatted names.
- Added ledger configuration, categories, session types, `LedgerHandler`, CRUD, interactive creation, and summaries.
- Connected ledger session routing, command routing, and help text. Chat supports entry, queries, edits, and statistics.
- Added a one-time `old_bill.csv` import script in `plugins/pendo/scripts`.
- Changed the ledger category “美容” to “服务” and updated the import script.

### Pendo routing, diary, and search

- Ledger edit commands use explicit field syntax to reduce parsing errors.
- Removed the ledger payment-method field to reduce redundant early-model attributes.
- Simplified the router with data-driven `COMMAND_META`.
- Improved diary commands and fixed Session writes in diary-template steps.
- Improved search with ledger support, type grouping, and richer display.
- Fixed the settings-view command and added missing switches.

### Core Session improvements

- Core `Session` supports dictionary-like access.
- Added plugin Session cleanup for lifecycle completion and canceled flows.

## 2026-03-22

### Pendo multi-node events

- Fixed multi-node event ordering and reminder display.
- Refactored time utilities so events, reminders, queries, and display share consistent time handling.
- Removed obsolete plans and design documents to reduce interference from historical drafts.

## 2026-03-21

### Reduced Pendo complexity

- Simplified reminder, event-handler, database, and AI-parser code.
- Clarified responsibilities across reminders, event processing, database access, and AI parsing.

### QingPet training and exploration

- Added an improvement design specification, review revisions, and an implementation plan.
- Interaction-gate messages show specific reasons and cover `sleep_pet`.
- Removed the friendship-point requirement for recall, which simplifies pet recall.
- Added training and exploration location constants.
- Improved training and exploration with effects from type, location, and personality.
- Pet cards show remaining travel time. Updated train/explore help text.

## 2026-03-13

### APOD, arxiv_filter, and xiaoqing_chat tests

- Fixed APOD decoding to reduce astronomy-content read failures.
- Updated arxiv_filter and continued training-code cleanup.
- Aligned goal derivation and fixed mock-test errors in xiaoqing_chat.
- Fixed related tests so astronomy, training, and chat modules remain testable after refactoring.

## 2026-03-11

### arxiv_filter training and inference structure

- Refactored directories to clarify training-data preparation, model training, and runtime inference.
- Fixed signin tests to keep plugin tests separate from training code.

## 2026-03-10

### v3.2.0 release

- Released `v3.2.0`.
- Fixed Pendo event-list formatting for stable multi-node and ordinary event display.
- Fixed asynchronous goal-state handling to reduce state mismatches in xiaoqing_chat.

## 2026-03-04

### Pendo event reminders and queries

- Each event has a reminder at its start time.
- Fixed multi-node event list filtering. An event appears only when at least one node is within the query range.
- Dynamic SQL column names use quotes, which supports field names that match SQL reserved words.

## 2026-03-03

### Local run scripts

- `run-bot.vbs` uses relative paths to support different local project directories.

## 2026-03-02

### Project migration and initial CI

- Migrated XiaoQing to the current master repository as the development baseline.
- Fixed CI unit-test paths to use `tests/` consistently.
- Fixed the CI job for plugin-marked tests so they execute in the plugin-test task.
- Updated README and project documentation with basic instructions for the migrated repository.
