# ✅ xiaoqing_chat release acceptance guide

[中文](xiaoqing_chat测试.md) | English

This guide gives testers and release maintainers a repeatable acceptance procedure for `xiaoqing_chat`. It covers automated tests, OneBot entries, command arguments, group participation, multimodal input, memory, AI routes, security, concurrency, and shutdown.

## ✅ Acceptance result

A release passes only when all these conditions hold:

1. All dedicated automated tests pass.
2. Normal, boundary, permission, and error inputs for `/xc` agree with the Manifest.
3. OneBot message segment checks and real group scripts pass.
4. Remote model, media, memory, and delivery commit checks pass.
5. Log, configuration, data isolation, concurrency, and shutdown checks pass.
6. The report includes environment, scope, evidence, defects, and a final result.

Any P0 or P1 defect fails the release gate. An explicit release disposition and retest evidence are necessary for P2 defects.

## ✅ Test environment

Record these details for each run:

- Git commit and workspace status.
- Python version and dependency installation results.
- OneBot implementation, version, HTTP/WS addresses, and test Bot QQ number.
- Test group number, administrator account, and standard member account.
- AI provider, model profile, route order, and modalities.
- Hash of `plugins/xiaoqing_chat/config/xiaoqing_config.json`.
- Start time, end time, and report directory.

Use a dedicated test group, test accounts, and an isolated data directory. Copy the configuration before execution. After execution, check byte-for-byte restoration, stable secrets hashes, released ports, and ownership of test data directories.

Use this report directory:

```text
test_reports/runs/plugins/xiaoqing_chat/<RUN_ID>/
```

Use a UTC timestamp and short Git hash for `RUN_ID`. Example: `20260807T120000Z-a1b2c3d`.

## 📌 Stage 1: static and automated gates

### Code and configuration checks

```bash
python -m compileall -q plugins/xiaoqing_chat
python -m pytest -q tests/plugins/xiaoqing_chat
```

Tests use isolated directories, model substitutes, and controlled tasks. Real models and QQ connections stay outside this suite.

| Area | Test modules | Coverage |
| --- | --- | --- |
| Commands and permissions | `test_xiaoqing_chat_commands.py`, `test_xiaoqing_chat_provider_scope.py`, `test_xiaoqing_chat_review_lifecycle.py` | All `/xc` subcommands, aliases, argument errors, group/Bot administrator permissions, session/global model overrides, and review session IDs |
| Invocation and participation | `test_xiaoqing_chat_planning_participation.py`, `test_xiaoqing_chat_reply_gate.py`, `test_xiaoqing_delivery_limiter_frequency.py`, `test_xiaoqing_chat_humanlike_delivery.py` | Explicit calls, coreference, participation cues, other addressees, low-information messages, rate limits, generation quotas, repeated message patterns, names, and timezones |
| Planning and goals | `test_xiaoqing_chat_goal_reset.py`, `test_xiaoqing_planning_retrieval_scoring.py`, `test_xiaoqing_chat_conversation_boundaries.py` | PFC actions, goal synchronization, long-gap transitions, and retrieval scores |
| Generation | `test_xiaoqing_prompt_builder.py`, `test_xiaoqing_chat_reply_generation*.py`, `test_xiaoqing_chat_generation_boundaries.py`, `test_bot_name_injection.py` | Prompt structure, name injection, context-limit rebuilding, review-timeout fallback, gateway errors, and public errors |
| Review | `test_reply_checker.py`, `test_reply_checker_grounding.py`, `test_reply_checker_risk_mode.py`, `test_reply_checker_social_boundaries.py`, `test_xiaoqing_chat_reply_completion.py` | Repetition, near-copying, consecutive speech, invented experience, semantic axes, evidence contracts, `always`/`risk`, persona identity, missing images, targeted rewrites, and fallback |
| Delivery and lifecycle | `test_xiaoqing_chat_turn_lifecycle.py`, `test_xiaoqing_reply_payload.py`, `test_xiaoqing_chat_shutdown.py`, `test_xiaoqing_chat_background_tasks.py`, `test_xiaoqing_chat_task_*.py` | Commit after acknowledgment, stale result removal, rollback, batching, background creation failure, deduplication, and shutdown waits |
| Media | `test_xiaoqing_chat_media_*.py`, `test_xiaoqing_chat_emoji_library.py` | Segment parsing, sources, OneBot retrieval, size/pixel/frame limits, vision retry/fallback, summary caches, emoji library, and outbound markers |
| Memory and storage | `test_xiaoqing_chat_memory_*.py`, `test_xiaoqing_chat_vector_store_validation.py`, `test_xiaoqing_chat_atomic_readers.py`, `test_xiaoqing_chat_store_base_helpers.py`, `test_xiaoqing_chat_cache_*.py`, `test_xiaoqing_chat_thinking_back.py`, `test_xiaoqing_chat_review_store.py`, `test_xiaoqing_chat_dedup_json_llm.py` | Atomic reads/writes, backup recovery, corrupt input, concurrent saves, reset races, vector indexes, cache expiry, and model JSON repair |
| Expression learning | `test_xiaoqing_chat_expression_store_persistence.py`, `test_xiaoqing_expression_retry_reflection.py`, `test_xiaoqing_chat_meme_learning.py` | Incremental expression/jargon merge, reflection retries, group-local meanings, explicit corrections, and resets during learning |
| Isolation and privacy | `test_xiaoqing_chat_tenant_scope.py`, `test_xiaoqing_log_privacy.py`, `test_xiaoqing_chat_audit_regressions.py` | Session isolation, log redaction, sensitive delivery exclusion, stale tasks after reset, and action-history limits |
| Configuration and runtime | `test_xiaoqing_chat_runtime_hardening.py`, `test_xiaoqing_chat_cache_lifetimes.py` | Configuration validation, load-failure fallback, reload, and cache lifecycle |
| Reply latency | `test_xiaoqing_chat_reply_latency.py` | Parallel reading/generation, matching review material, rewrite reuse, rereading changed material, failed-read exclusion, kept reads after review timeout, `risk` behavior, and elapsed-time delay deduction |
| Experiments and scripts | `test_xiaoqing_chat_anthropomorphic_experiment.py`, `tests/tooling/test_xiaoqing_chat_quality_script.py` | Humanlike experiment matrices, delivery receipts, quality-probe scripts, and machine gates |

Plugin contract tests in `tests/plugins/contracts` cover log redaction and call/signature agreement. Run them with the plugin suite:

```bash
python -m pytest tests/plugins/xiaoqing_chat tests/plugins/contracts -q
```

### Project UAT plan

Generate the plan. Check stages, ports, and output directories:

```bash
python scripts/run_full_uat.py \
  --plan-only \
  --include-chat-quality \
  --matrix-plugins xiaoqing_chat
```

Run the command matrix and chat quality gate:

```bash
python scripts/run_full_uat.py \
  --include-chat-quality \
  --matrix-plugins xiaoqing_chat \
  --output test_reports/runs/project/<RUN_ID>
```

Add `--include-external` when acceptance includes external AI and network services. Final reports are in the output directory's `reports/` folder.

## ⌨️ Stage 2: service and OneBot entries

### Startup checks

1. Run `python main.py` with the release configuration.
2. Wait for the plugin load log.
3. Check that `xiaoqing_chat` is loaded.
4. Check HTTP and WS listener addresses against the test configuration.
5. Check the OneBot connection and Bot self-information query.

### Event baseline

Send one standard private event and one standard group event through HTTP or WS. Record these details:

- Request status and request ID.
- Router and Dispatcher selections.
- Messages sent to OneBot actions and action acknowledgments.
- Delivery acknowledgments.
- Applicable session state changes.

Send the same `message_id` again. Check deduplication and state counts.

## ⌨️ Stage 3: command matrix

Use `plugins/xiaoqing_chat/plugin.json` as the authoritative command list. Test each node with these inputs:

- Standard examples.
- Chinese aliases.
- Minimum and maximum arguments.
- Missing arguments.
- Extra arguments.
- Unknown subcommands or enumeration values.
- Standard member, group administrator, group owner, and Bot administrator permissions.
- Private and group contexts.

### Top-level commands and help

| Case | Expected result |
| --- | --- |
| `/xc 你好` | Explicit-invocation reply flow |
| `/xc help` | Complete structured directory |
| `/xc 帮助` | Same result as help |
| `/xc help extra` | Argument error and usage |
| `/xc <未知子命令>` | Chat reply flow, or an explicit result under the current routing contract |

### Session management

| Case | Expected result |
| --- | --- |
| Private `/xc reset` | Clear this private session |
| Group `/xc reset` | Request confirmation and keep state |
| Standard group member `/xc reset confirm` | Permission explanation |
| Group administrator `/xc reset confirm` | Clear the current group session |
| Private `/xc reset confirm` | Explain the group-only context |
| `/xc reset maybe` | Argument error or confirmation prompt |
| `/xc stats` | Context, expression, jargon, action, and runtime statistics |
| `/xc stats extra` | Argument error |

Before reset, write short history, long-term memory, Goal, PFC, actions, expressions, jargon, and reflection state. After reset, check each item within the current chat scope.

### Status queries

| Case | Expected result |
| --- | --- |
| `/xc brain` | Deep-dialogue switch and parameters |
| `/xc config` | Rate-limit, memory, expression, and model summary |
| `/xc memory 关键词` | Ranked results in the current chat scope |
| `/xc memory` | Usage |
| `/xc expression` | Current chat expressions |
| `/xc jargon` | Jargon visible to the current chat |

### Model management

| Case | Expected result |
| --- | --- |
| `/xc model` | Aliases, models, providers, current selection, and fallback order |
| Group administrator `/xc model <名称>` | Pin the current session chat model |
| Standard member `/xc model <名称>` | Permission explanation |
| `/xc model default` | Clear the session override |
| Bot administrator `/xc model global <名称>` | Pin the global runtime chat model |
| Group administrator `/xc model global <名称>` | Bot administrator permission explanation |
| `/xc model global default` | Clear the global override |
| `/xc model <未知名称>` | Available names |

After switching, trigger chat, reasoning, checker, and vision calls. Check that the override affects only the `chat` route. Restart the Bot. Check that model selection returns to configuration values.

### Reflection review

| Case | Expected result |
| --- | --- |
| `/xc review ok <id>` | Confirm the current step or advance |
| `/xc review no <id>` | Close the session |
| `/xc review answer <id> <内容>` | Apply rules, goals, or policies |
| `/xc review close <id>` | Close the session |
| Wrong ID | Scope or expiry explanation |
| Wrong action, missing content, extra arguments | Usage or field explanation |
| Standard member execution | Permission explanation |

Test the same chat, different chats, expired sessions, and concurrent handling of one review ID.

## 💬 Stage 4: message segment matrix

Send each message through a real OneBot entry, `observe_message()`, and `handle_smalltalk()`.

| Segment | Checks |
| --- | --- |
| `text` | Text order, whitespace normalization, and context records |
| `at` | Bot mentions, other mentions, and invocation reasons |
| `reply` | Bot targets, member targets, missing message IDs, and history lookup |
| `face` | QQ face descriptions and quiet decisions for low-information content |
| `mface` | Source validation, markers, collection, and caching |
| `image` | Downloads, size, pixels, animation, vision descriptions, and caching |
| Mixed segments | Original order and text/media association |

Boundary samples include empty text, punctuation only, multiple images, oversized images, animations, multiple `at` segments, invalid replies, and repeated media URLs.

## 📌 Stage 5: invocation and participation

### Explicit invocation matrix

Cover these scripts:

- Standard private messages.
- Explicit `/xc` messages.
- Group @ mentions of the Bot.
- Text with the Bot name.
- A name-only call followed by a question from the same user.
- A reply to the Bot's previous message.
- `她`, `他`, and `ta` coreference after a Bot history anchor.
- Pronouns after the history anchor changes to another member.

Record `forced`, `reason`, `direct_mentioned`, `coreference_mentioned`, and `reply_to_bot`.

### Standard participation matrix

Cover these state combinations:

- Base probability success and failure.
- Questions, invitations, and openings for the whole group.
- Topics explicitly addressed to other members.
- Previous Goal matches and standard continuation.
- Follow-up questions in active topics.
- QQ faces and punctuation only.
- Minimum intervals, per-minute limits, and consecutive-reply cooldowns.
- All Heartflow weighted signals.
- PFC reply, observe, wait, and goal changes.
- Global, session, and user concurrency quotas, plus daily user usage.

Use a fixed random seed for probability cases. Gate logs must give stable reason codes.

## 🧠 Stage 6: real models and humanlike quality

### Quality probe

Run while the service is active:

```bash
python scripts/run_xiaoqing_chat_quality.py \
  --endpoint http://127.0.0.1:12000/event \
  --chat-data-dir test_reports/runs/plugins/xiaoqing_chat/<RUN_ID>/data/xiaoqing_chat \
  --output test_reports/runs/plugins/xiaoqing_chat/<RUN_ID>/quality.json
```

The port belongs to the dedicated test Bot. `--chat-data-dir` selects that Bot's actual isolated plugin data directory. Check that the directory exists before execution.

The probe uses `X-XiaoQing-Response-Mode: actions` for debug action previews. Standard OneBot HTTP event responses are empty objects. Action acknowledgments supply actual delivery evidence. Use a new report path. The script protects existing reports.

### Large-group humanlike experiment

```bash
python -m plugins.xiaoqing_chat.experiments.anthropomorphic_group \
  --mode real \
  --run-id <RUN_ID> \
  --groups 20 \
  --rounds-per-group 150
```

`--mode` has three values. `matrix` generates only the experiment matrix. `dry-run` generates reproducible placeholder results to check the framework and scoring with plugin calls disabled. `real` calls real models.

Outputs include `anthropomorphic-group-matrix.json`, JSONL results, group transcripts, and a summary. The default directory is `test_reports/runs/plugins/xiaoqing_chat/<RUN_ID>/`. `--output-dir` selects another directory. `--seed` fixes the workload.

`--groups`, `--min-users`, `--max-users`, and `--rounds-per-group` set scale. `--max-real-turns` limits remote turns. `--checkpoint-interval` sets checkpoint spacing.

### Multi-user group evaluation

```bash
python scripts/run_xiaoqing_chat_live_eval.py                  # All scripts in parallel + model review
python scripts/run_xiaoqing_chat_live_eval.py --suite otaku     # Run a selected script. The option can repeat.
python scripts/run_xiaoqing_chat_live_eval.py --judge-only <DIR>
```

This script runs independently of the Bot service. It reads real model routes from `config/config.json` and `config/secrets.json`. Each script uses an independent data directory and the complete `observe_message()` → `handle_smalltalk()` path. Sending uses simulated successful receipts. Production data and QQ groups remain outside this run.

Five virtual members, 阿夏, 阿禾, 阿白, 阿梨, and 阿柚, speak at scripted intervals. Messages can include images, memes, QQ faces, @ mentions, and replies. `at="bot"` mentions XiaoQing. Each turn records image-reading results in `media_logs` for analysis of image/reply mismatches.

| Suite | Focus |
| --- | --- |
| `otaku` | Humanlike behavior, anime-group chat, jokes, opinions, AI-identity questions, and repetition |
| `addressee` | Parallel topics, named members, mentions of others, multiple addressees, and evaluations of XiaoQing |
| `participation` | Emotion, good news, message floods, silence, invitations, and image requests |
| `multimodal` | Image reading, meme meanings, old-image references, outbound memes, broken images, and unseen images |
| `notice` | Text-heavy laboratory reports, water outage notices, Web notices with UI and URLs, and complete examination schedules. Mention XiaoQing with an image. Then ask about deadlines, email, URLs, and classrooms. |
| `persona` | Personal questions, preference memory and recall, knowledge, prompt extraction, and long-text requests |
| `regress` | Calculations and outbound asset regressions, including price conversion and expected meme delivery |

Each turn first runs deterministic rules. Findings enter the report.

| Rule | Meaning |
| --- | --- |
| `missed_reply` | An expected reply is absent |
| `intrusion` | A reply occurs when the script specifies silence |
| `fallback` | Review exhaustion uses a fallback sentence, identified by `reply.check.exhausted.*` or exact `ALL_FALLBACK_TEXTS` matching |
| `media_doubt` | Image reading succeeds, but the reply claims uncertainty or failed image loading |
| `breaks_character` | The reply admits AI identity or mentions prompts |
| `markdown` | Standard chat contains Markdown |
| `too_long` | A turn exceeds 90 characters |
| `verbatim_repeat` | The reply repeats its own earlier text exactly |
| `checker_rejected` / `checker_infra` | Candidate rejection / unavailable review model |
| `exception` / `slow` | Pipeline exception / turn above 40 seconds |

The `reasoning` route then reviews each turn against the script's judge-only `note`. That note stays outside character context. It gives five scores from 1–5: `humanlike`, `participation`, `addressee`, `multimodal`, and `consistency`. `multimodal` is empty for scripts without images.

Outputs go to `test_reports/runs/plugins/xiaoqing_chat/<时间>-liveeval-<commit>/`. Each suite has a JSONL with inputs, replies, checker records, and step logs. Shared outputs are `summary.json` and `report.md`.

`--seed` fixes randomness. `--no-judge` runs scripts and deterministic rules only. `--judge-only <DIR>` reviews existing results again. `--out-dir` selects the output directory.

For low scores, read the applicable JSONL turns first. Correct the script or asset when its material is ambiguous, such as an overly abstract image.

### Real group scripts

Cover at least these scenarios:

1. Multiple members chat quickly. The Bot selects suitable turns.
2. Two members talk together. The Bot keeps addressee identification.
3. An open question invites the whole group.
4. @ mentions, names, replies, and coreference sustain conversation.
5. Images, memes, QQ faces, and text appear together.
6. A new topic follows a long gap.
7. Users recall preferences, nicknames, and previous agreements.
8. Science, numbers, and units trigger reasoning.
9. Uncertain facts, third-party information, and real-world promises trigger reply checking.
10. High message rates, repeated text, and concurrent users occur.

Record input, invocation reason, participation decision, planner action, model profile, reply, media segments, check results, latency, and delivery status for each turn.

### Quality scores

Give each group script scores from 1–5 for these dimensions:

- Participation timing.
- Context continuity.
- Persona consistency.
- Natural conversation.
- Information accuracy.
- Group addressee identification.
- Multimodal naturalness.
- Silence selection.

Also record defects for frequent follow-up questions, template-like speech, repetition, wrong speakers, wrong media, unsupported facts, and message floods.

## 🧠 Stage 7: memory, planning, and learning

### Session and long-term memory

1. Write user and assistant messages through continuous conversation.
2. Keep assistant state pending before delivery acknowledgment.
3. Commit assistant memory and action records after acknowledgment.
4. Create a long gap. Check continuous segments, Goal, PFC, Thinking Back, and summary refresh.
5. Use `/xc memory` to check vector ranking, thresholds, and chat scope.
6. Create an empty direct retrieval result. Trigger the tool agent with an explicit backward reference.
7. Restart the Bot. Check recovery of confirmed state.

### Profiles, expressions, and jargon

Cover fact-extraction checkpoints, duplicate messages, profile updates, expression counts, jargon scope, manual review, and selector injection. Test the current chat, another group, and a private chat separately.

### Storage robustness

Create these inputs for each JSON store:

- Valid primary file.
- Valid backup file.
- Truncated JSON.
- Wrong root object type.
- Wrong field types.
- `NaN`, `Infinity`, and out-of-range numbers.
- Concurrent saves and restart recovery.

Check atomic publication, backup selection, field-level fallback, dirty flags, and debounced save counts.

## 🩺 Stage 8: AI and media failures

### AI routes

Inject these conditions separately into `chat`, `reasoning`, `checker`, and `vision`:

- First-profile success.
- First-profile timeout followed by next-profile success.
- Rate limits and server errors.
- Authentication and argument errors.
- Empty responses, malformed JSON, and wrong root types.
- Exhausted total timeout.

Check route order, retry budgets, total timeout, error categories, and user-visible results.

### Reply checking

Cover deterministic hard/soft results, semantic hard/soft results, regeneration, replanning, proactive participation exhaustion, and explicit-invocation exhaustion. Check candidate state and final commit state in each case.

Independent reviewers read complete inputs and replies to assess communication constraints. Cover direct questions, indirect statements, quotations, conditionals, and equivalent rewrites. Quality probes keep material for manual review. `machine_gate_passed` represents machine metrics only. `semantic_review_required` marks independent semantic review requirements. `semantic_acceptance_passed` stays empty until that review.

Missing-image cases use text-only events that refer to a specific screenshot. Check that replies describe the failed image naturally, as a group member would. Keep unsupported agreement with unseen image descriptions outside the reply. Check evidence for colors, layout, text, and objects.

Use real historical images as separate positive cases. Check that backward references can use their summaries. The stale-topic probe's `current_turn_discloses_missing_image` records missing-image disclosure. Old-topic exclusion remains a separate check.

### Media

Cover vision timeout, download timeout, source restrictions, decoding errors, cache hits, similar-asset deduplication, disk quotas, TTL cleanup, and empty marker matches. Check agreement between text, media segments, and asset-use records.

## 🔐 Stage 9: security, concurrency, and shutdown

### Permissions and isolation

- Agreement between event `user_id` and `context.principal`.
- Private, group administrator, group owner, and Bot administrator capabilities.
- Cross-group memory, review, expression, jargon, and media access.
- Plugin AI route and provider credential boundaries.
- Redaction of tokens, API keys, URL queries, user text, and identifiers in logs.

### Concurrency

- Multiple simultaneous messages in one chat.
- Simultaneous messages in multiple chats.
- One user's generation quota across chats.
- Reset during generation.
- Reset during memory save.
- Out-of-order and duplicate delivery receipts.
- Background learning task creation failures and duplicate scheduling.

Check lock scope, generation quota release, deduplication, state commit order, and store integrity.

### Shutdown

1. Create debounced saves, media refinement, and learning tasks.
2. Trigger plugin shutdown.
3. Check the change in task acceptance state.
4. Check bounded waits, cancellation, and exception records.
5. Check that all dirty stores flush to disk.
6. Start again. Check state recovery.

## ✅ Defect classes

| Class | Definition | Examples |
| --- | --- | --- |
| P0 | Critical data, security, or lifecycle failure | Cross-group disclosure, credential disclosure, data corruption, or main-process crash |
| P1 | Core function failure | Missing explicit-invocation reply, permission bypass, wrong state commit, or complete model-chain failure |
| P2 | Major experience or boundary defect | Poor participation timing, wrong media, absent error messages, or persistence recovery differences |
| P3 | Local display or maintenance defect | Wording, log fields, or rare formatting problems |

## ✅ Report template

Each acceptance report includes these sections.

### Environment

| Item | Value |
| --- | --- |
| RUN_ID | |
| Git commit | |
| Python | |
| OneBot | |
| Test groups and accounts | |
| AI routes | |
| Configuration hash | |

### Stage results

| Stage | Status | Cases | Failures | Evidence path |
| --- | ---: | ---: | ---: | --- |
| Automated gate | | | | |
| OneBot entries | | | | |
| Command matrix | | | | |
| Message segment matrix | | | | |
| Invocation and participation | | | | |
| Humanlike quality | | | | |
| Memory and learning | | | | |
| AI and media failures | | | | |
| Security, concurrency, and shutdown | | | | |

### Defects

| ID | Class | Scenario | Reproduction steps | Actual result | Expected result | Fix commit | Retest evidence |
| --- | --- | --- | --- | --- | --- | --- | --- |

### Release result

- Gate status: pass / fail.
- P0 count.
- P1 count.
- P2 count and disposition.
- Automated report path.
- OneBot evidence path.
- Humanlike experiment summary path.
- Configuration and data integrity results.
- Test owner and completion time.

The final result uses actual evidence from this run. Retests use a new `RUN_ID`. The defect table links original defects, fixes, and retest evidence.
