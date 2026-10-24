# 🏗️ xiaoqing_chat architecture

[中文](ARCHITECTURE.md) | English

This document describes modules, turn data flow, reply review, state commits, and persistence for plugin maintainers. See [README.en.md](README.en.md) for functions, commands, and settings. See the [acceptance guide](xiaoqing_chat测试.en.md) for tests.

## 🏗️ Responsibility boundaries

The plugin uses XiaoQing Core and does these functions:

1. Identify the relationship between messages and the character. Decide whether and when to participate.
2. Build continuous conversation segments, long-term memory, profiles, goals, and media context.
3. Plan replies, observation, waiting, and goal changes.
4. Generate and check candidates through `chat`, `reasoning`, `checker`, and `vision` routes.
5. Convert text and media intents to ordered OneBot batches.
6. Commit memory, planning, and learning state after delivery acknowledgment.

Core supplies configuration snapshots, authenticated principals, AI credentials and transport, plugin data directories, delivery receipts, lifecycle control, and execution budgets. The plugin accesses these through `PluginContext`, `context.capabilities`, and Core services.

```text
XiaoQing Core
  ├─ Router ─────────────── /xc ─────────────────────────┐
  ├─ Dispatcher ─────────── observe_message / handle_smalltalk / call_bot_name_only
  ├─ Core service ───────── core.observe_outgoing_action │
  ├─ Service calls ───────────── agent.handle_turn / agent.report_delivery → xiaoqing_agent
  ├─ AI capability ──────── chat / reasoning / checker / vision
  ├─ onebot_media ───────── get_msg / get_image         │
  └─ Delivery ───────────── ordered batches + receipt    │
                    │                                    │
                    ▼                                    ▼
xiaoqing_chat
  ├─ Entries and coordination        main / handlers / commands
  ├─ Invocation and participation        attention_gate / participation / frequency_control / generation_limiter
  ├─ Three turn phases        turn_generation → reply_plan + reply_generator → turn_delivery
  ├─ Models and review        llm/
  ├─ Planning              planning/
  ├─ Memory and knowledge        memory/
  ├─ Expression learning          expression/
  ├─ Media              media/ + media_registry + reply_media + reply_payload
  └─ Runtime state and storage    runtime_state / task_scheduler / store_base / store_binding
```

## 📂 Module map

### Top level

| Module | Function |
| --- | --- |
| `plugin.json` | Manifest: `/xc` tree, `onebot_media`, `core.observe_outgoing_action`, `agent.handle_turn`, `agent.report_delivery`, and reload files |
| `main.py` | `init`/`shutdown`, `/xc` routing, and Core observation entries |
| `handlers.py` | Observation, chat gates, turn coordination, generation quotas, `call_bot_name_only`, and `observe_outgoing_action` |
| `agent_bridge.py` | Run agent decision and chat generation together for explicit calls. On takeover, cancel generation, release capacity, execute the agent, record replies with message IDs, and report delivery. Otherwise, use the generated chat reply. |
| `commands.py` | Statistics, reset, configuration, memory, expressions, jargon, review, and model selection |
| `handler_context.py` | Per-turn runtime, state, chat ID, Bot name, and secrets snapshot. Also supplies `handle_errors`. |
| `chat_event.py` | Read conversation ID, sender, and character names from OneBot events |
| `chat_runtime.py` | Configuration reload, AI route context, `llm_call_config`, block rules, and session locks |
| `attention_gate.py` | Explicit @, name, name follow-up, character reply, and coreference detection |
| `participation.py` | Group invitations, image requests, open questions, cheering, emotional sharing, repetition, low-information content, and other addressees |
| `frequency_control.py` | Hard limits, participation probability, Heartflow additions, silence compensation, and `ReplyGateDecision` |
| `generation_limiter.py` | Global, session, and user concurrency quotas, plus daily user quotas |
| `brain_chat.py` | Private deep dialogue: append the persona `〔深度对话〕` section after speech rules. Set thinking level, context length, and temperature. |
| `persona.py` | Resolve names and parse `config/persona.md` into facts, chat style/examples, agent sections, and mood |
| `smalltalk_models.py` | `PreparedSmalltalkTurn` and `GeneratedSmalltalkTurn` phase models |
| `turn_generation.py` | Record user messages under the session lock. Generate outside it through forced, PFC, or direct paths. |
| `reply_plan.py` | `ReplyGenerationPlan`: collect context blocks, reaction rules, goals, and persona boundaries once |
| `reply_generator.py` | Attempt loop: independent reading, requests, local checks, media resolution, review, and context reduction |
| `reply_draft.py` | Drafts, segment normalization, outbound media, and attempt state |
| `reply_fallbacks.py` | Targeted rewrite prompts and explicit-call fallback sentences in `ALL_FALLBACK_TEXTS` |
| `turn_delivery.py` | Typing delay, stale-result removal, batched sending, post-delivery commit, and rollback |
| `post_reply_tasks.py` | Summaries, fact extraction, expression learning, and strategy review after successful delivery |
| `reply_media.py` | Media integration and asset-use records |
| `reply_payload.py` | Convert message parts to display text and deliverable batches |
| `message_parts.py` | Lossless conversion between structured text/media parts and `content`/`media_items` |
| `media_registry.py` | Compact stable media references in chat history |
| `context_builder.py` | Memory, profiles, knowledge, expressions, and tool context |
| `runtime_state.py` | Plugin stores, session state, background tasks, quotas, and model overrides |
| `task_scheduler.py` | Background registration, debounced flushing, and shutdown cleanup |
| `store_base.py` / `store_binding.py` | Shared JSON store protocol and binding to `context.data_dir` |
| `logging_utils.py` | Structured `log_step` output and redaction |
| `constants.py` | Stable constants shared by modules |

### `llm/`: model calls and review

| Module | Function |
| --- | --- |
| `gateway.py` | Core AI adaptation: routes, pinned models, and response extraction |
| `llm_config.py` | `LLMCallConfig` timeouts, retries, and sampling. `bounded()` restricts temperature/output. `as_kwargs()` supplies gateway arguments. |
| `prompt_builder.py` | Build reply prompts from persona boundaries, history, and current media |
| `postprocess.py` | Keep reply content and split bubbles at natural boundaries |
| `communication_constraints.py` | Original-message requirements for silence, sentence/item counts, length, and group definitions |
| `media_evidence.py` | Image evidence, attachment state, missing-image notes, `INBOUND_MEDIA_MARKER_RE`, and `marker_visible_speech` |
| `local_checks.py` | Repetition, near-copying, consecutive speech, invented experience, identity consistency, media-only replies, and semantic-review requirements |
| `reply_checker.py` | Local rules, model review, evidence interpretation, then explicit length checks after other checks pass. Insufficient length is soft. `TurnReadingCache` stores independent readings. |
| `evidence.py` | Check that model claims have original-text sources with matching subjects and tone |
| `arithmetic.py` | Numerical evidence for portions, discounts, unit conversion, and two-term addition/subtraction |
| `check_result.py` | `ReplyCheckResult`, `ReplyRejected`, and standard rejection results |
| `summarizer.py` | Incremental topic summary compression |

### `planning/`: action planning

| Module | Function |
| --- | --- |
| `pfc_engine.py` | PFC session preparation, actions, waits, reconsideration, and state commits |
| `pfc_action_planner.py` | Convert context to bounded action plans |
| `pfc_goal_analyzer.py` | Update short-term goals from visible conversation and action history |
| `pfc_state.py` | PFC persistence and concurrent writes |
| `pfc_utils.py` | Extract structured lists from model responses |
| `planned_action.py` | Immutable planned actions |
| `goal_state.py` | Conversation goals and active topics |
| `heartflow.py` | Reply rhythm and participation scores |
| `action_history.py` | Persist confirmed actions, with 200 records per session |

### `memory/`: memory and knowledge

| Module | Function |
| --- | --- |
| `memory.py` | `StoredMessage` and persistent recent history per session |
| `local_ids.py` | Assign, find, and replace session-local `mN` message IDs |
| `memory_db.py` | Long-term documents, profiles, and vector indexes |
| `vector_store.py` | Local hash-vector index and document/matrix consistency cache |
| `memory_retrieval.py` | Scoped memory context with sources and a retrieval agent |
| `person_profile.py` | Profiles by session and principal, plus reset generations |
| `knowledge_extract.py` | Extract personal facts with subject evidence from original speech |
| `fact_extraction_checkpoint.py` | Fact extraction checkpoint |
| `knowledge_base.py` | Administrator-approved knowledge files and bounded all-or-nothing indexing |
| `topic_summary_cache.py` | Topic summary cache |
| `thinking_back.py` | Recent recall cache |
| `review_sessions.py` | Human review sessions and reply policies |

### `expression/`: expressions and jargon

| Module | Function |
| --- | --- |
| `bw_expression_learner.py` | Extract, review, and store expressions from user messages |
| `bw_expression_store.py` | Atomic expression persistence and incremental merge |
| `bw_expression_reflector.py` | Sample pending expressions and request operator confirmation |
| `bw_reflect_tracker.py` | Reflection queue and operator decisions |
| `bw_jargon_miner.py` | Extract jargon and complete local definitions |
| `bw_jargon_store.py` | Persist session or global jargon |
| `bw_message_recorder.py` | Learning checkpoint to prevent duplicate extraction |
| `expr_utils.py` | Shared helpers |

### `media/`: media

| Module | Function |
| --- | --- |
| `event_media.py` | Parse inbound OneBot media into bounded context with privacy protection |
| `event_media_analysis.py` | Vision pipeline and meme-background prompts |
| `sources.py` | Downloads, OneBot retrieval, data URLs/base64, local-path authorization, file storage, and byte limits |
| `loading.py` | Blocking-task limits, bounded reads, source identification, image detection, and animation frame assembly |
| `markers.py` | `[图片：…]` / `[表情包：…]` markers, description quality, and fallback descriptions |
| `render_cache.py` | Summaries by media hash, with capacity, expiry, and prompt-version checks |
| `models.py` / `settings.py` | Data types, supported scope, media configuration, and inbox directories |
| `emoji_library.py` | Privacy-scoped emoji library and derived-metadata repair |
| `marker_resolver.py` | Resolve outbound intents to authorized real assets |
| `qq_face.py` / `qq_face_catalog.py` | QQ face descriptions and merged built-in/runtime catalogs |

### Other directories

| Directory | Content |
| --- | --- |
| `config/` | `config.py` Pydantic models, cross-field constraints, load order, and `xiaoqing_config.json` |
| `experiments/` | `anthropomorphic_group.py` large-group experiment runner |
| `utils/` | `json_parsing.py` for constrained JSON repair, `text.py` for `clip_text`, and `wall_clock.py` for Core-timezone formatting |

## ⌨️ Framework entries

| Entry | Caller | Function |
| --- | --- | --- |
| `init(context)` | PluginManager | Accept background tasks, bind stores, and load the media registry |
| `handle(command, args, event, context)` | Router | Route `/xc`. Send other content to `handle_smalltalk` as an explicit invocation. |
| `call_bot_name_only(context)` | Dispatcher | Give a short name-only response and register a 60-second follow-up window |
| `observe_message(clean_text, event, context)` | Dispatcher | Record inbound messages and original segments |
| `handle_smalltalk(clean_text, event, context)` | Dispatcher | Chat decisions and reply processing |
| `observe_outgoing_action(action, context)` | Core service | Record other plugins' character messages. Skip this plugin's replies, already committed with message IDs. Skip sensitive actions. |
| `shutdown(context)` | PluginManager | Stop task acceptance, flush, cancel, and wait for background tasks |

## 💾 Turn data flow

```text
OneBot event
  │
  ▼
observe_message
  ├─ Keep original segments. Rebuild text and media markers.
  └─ Write the scoped user message once.
  │
  ▼
handle_smalltalk → _prepare_smalltalk_turn
  ├─ Stop-chat request / blocked word -> end
  ├─ Long gap -> switch short-term state. Keep raw history.
  ├─ attention_gate.decide_attention        Explicit invocation?
  ├─ frequency_control.decide_reply         Standard participation: hard limits -> probability -> Heartflow -> silence adjustment
  ├─ Record goals observed in this turn.
  └─ PreparedSmalltalkTurn
  │
  ▼
agent_bridge（explicit invocation with agent enabled）
  ├─ Start agent decision and chat generation together.
  ├─ Chat decision / unavailable agent -> use the generated reply.
  └─ Agent takeover -> cancel generation and release capacity -> agent act -> reply and receipt to Core
  │
  ▼
generation_limiter.admit                    Global / session / user / daily quotas. Queue session capacity. Prefer the newest turn.
  │
  ▼
turn_generation.generate_smalltalk_turn
  ├─ Under the session lock: record the user message and get a PFC snapshot.
  ├─ Fetch memory in the background.
  └─ One of three paths:
       forced  → Draft directly
       planner → run_pfc_once → Call drafting when the planner selects speech
       direct  → Draft directly when planning is disabled
         │
         ▼
       reply_plan.build → reply_generator attempt loop
         ├─ Read the turn in always mode, parallel to generation. Reuse across attempts.
         ├─ prompt_builder → gateway(chat) → postprocess
         ├─ marker_resolver Resolve media intents
         ├─ reply_checker：Local rules + semantic review
         └─ Rejected -> reply_fallbacks: targeted rewrite / replan / fallback / silence
  │
  ▼
turn_delivery.finalize_smalltalk_turn
  ├─ Check staleness: new messages during typing or session reset.
  ├─ Typing delay: target minus elapsed time. Send batches with bubble intervals.
  ├─ Delivery acknowledgment -> commit assistant messages with all IDs, PFC, goals, Heartflow, and actions.
  │                 → post_reply_tasks：Summaries, fact extraction, expression learning, and strategy review
  └─ Delivery rejection / exception -> roll back pending state
```

Phases pass `PreparedSmalltalkTurn`, `GeneratedSmalltalkTurn`, and `ReplyPayload` models. A planner `wait` or `listening` action ends the turn immediately with silence. Core serializes events by group and user. Waiting inside the handler would delay that user's next message and keep session generation capacity.

Rejected standard participation turns write back the PFC snapshot. The next turn starts from the new state.

## 📌 Invocation and participation

`attention_gate.decide_attention` returns `AttentionDecision`:

| Field | Meaning |
| --- | --- |
| `forced` | Explicit invocation enters generation directly |
| `force_reason` | Invocation source |
| `mentioned` / `direct_mentioned` | Name or @ match |
| `coreference_mentioned` | Contextual coreference with a recent character anchor |
| `reply_to_bot` | Quoted `message_id` belongs to character history |

`frequency_control.decide_reply` uses this fixed order for standard participation:

1. `_hard_limit_reason` checks intervals, per-minute limits, consecutive cooldowns, low-information content, and other addressees. Active topics and follow-ups use shorter intervals.
2. `_participation_probability` starts with base probability. Participation cues, active topics, and active follow-ups raise it to their respective limits. It selects the maximum.
3. Heartflow uses the positive part of `score - base_score`. The update is `probability + (1 - probability) × Δ`.
4. `_streak_adjusted` applies ×1.2 after 3 silent turns, capped at 0.90. After 5 turns, it applies ×1.4, capped at 0.95.
5. A random draw determines the result. `ReplyGateDecision` stores the result, probability, draw, and silence count. `smalltalk.no_reply` logs these fields.

Core extracts text segments for the message body. `handlers._label_other_mentions` restores other members' @ segments as `@名字` at their original positions. Effective text, media composition, and history use this restored text. Hard limits, participation cues, the planner, and reply prompts use it to identify addressees.

Private deep dialogue skips steps 2–4. After admission, enabled planning uses PFC. Disabled planning drafts directly. Participation cues affect probability only.

## 🧠 Planning and generation

PFC runs `run_pfc_once` once per turn through `planning/pfc_engine.py`. It reads conversation segments, action history, goals, and knowledge. `pfc_action_planner` selects an action through `reasoning`:

| Action | Result |
| --- | --- |
| `direct_reply` / `send_new_message` | Call `_TurnGenerator.planner_reply` to draft with the action mode |
| `wait` / `listening` | End immediately with silence. Record `wait_seconds` in logs only. |
| `rethink_goal` | Update goals through `pfc_goal_analyzer` |
| `fetch_knowledge` | Retrieve knowledge, then plan again |
| `end_conversation` | End the topic and set state to ended |
| `block_and_ignore` | Ignore this turn |

Planner parse failure gives `direct_reply` in private chat and `wait` in groups. Planner timeout is the smaller of `pfc_planner_timeout_seconds` and `timeout_seconds`. Consecutive failures cause backoff.

`reply_plan.py` and `reply_generator.py` draft replies:

1. `ReplyGenerationPlan` collects history, memory, profiles, expressions, jargon, knowledge, keyword rules, goals, persona boundaries, and foreground call parameters. Retries reuse it.
2. `llm/prompt_builder.py` builds messages. `llm/gateway.py` calls `chat` through the plugin AI capability. Session/global overrides use `pinned_model`.
3. `llm/postprocess.py` cleans and segments output. `media/marker_resolver.py` resolves `[想发…]` markers.
4. `llm/reply_checker.py` reviews the candidate. `reply_fallbacks.py` selects the next step after rejection.
5. An upstream `request_too_large` halves history and invalidates retrieved memory. Generation then retries once.

Background tasks use `LLMCallConfig` from `llm_call_config(cfg, foreground=False)`. Each task calls `bounded(max_temperature, max_tokens, min_tokens)` to restrict sampling and output. Core AI capability manages credentials, HTTP, timeouts, retries, and profile fallback.

## ✅ Reply review

### Local rules: `llm/local_checks.py`

`heuristic_check` does these checks in sequence:

1. Check for an empty reply.
2. Compare with the latest `max_repeat_compare` character messages for exact matches or similarity at least `similarity_threshold`.
3. Check exact repetition of a recently spoken complete sentence of at least 8 characters.
4. Remove `[…]` markers and punctuation. Check similarity of at least 0.75 against the latest 6 character messages.
5. Apply that `_near_copy_of_recent` check only for at least 12 characters.
6. Check for `max_assistant_in_row` consecutive character messages.

A reply with one media item skips repetition comparison.

`grounded_self_history_check` checks evidence for the character's experiences, relationships, and recent situation. `grounded_context_history_check` checks evidence for members' experiences. `persona_identity_consistency_check` compares identity claims, including gender, with the persona. `media_meta_reply_check` detects media-only answers to questions for which text is necessary. `requires_llm_semantic_check` selects remote review in `risk` mode.

### Semantic review: `llm/reply_checker.py`

Review rules and JSON material use separate messages. The reviewer lists claims and evidence before its decision:

| Axis | Fields |
| --- | --- |
| Context | `context_coherent`, `context_claims`, with conversation evidence in `source_ids` |
| Speaker | `speaker_correct` |
| Communication requirements | `instruction_followed` |
| Facts | `factually_plausible` |
| Persona | `persona_grounded`, `persona_claims`. Human identity and playful deflection match the persona. AI/model/program admission or mentions of prompts, system settings, and instructions produce false. |
| Image evidence | `image_evidence_respected`, `visual_claims` |
| Template-like speech | `non_template` |

`llm/evidence.py` checks each claim's original source, subject, and tone. `llm/arithmetic.py` checks numerical derivations. With `enable_semantic_checker`, an independent read receives original speech only. It then compares stance, subject, time, and completion state with the candidate.

The two candidate reviews run in parallel under `reply_check.timeout_seconds`. `TurnReadingCache` manages independent-read timeout reuse and task cleanup. Confirmed hard errors from either path take effect. Conclusions inside a `json_object` wrapper are also parsed.

Independent reading uses original dialogue, the current message, speaker, group terminology, and character name. It is independent of candidates. `TurnReadingCache` caches one reading for this material.

In `always` mode, `reply_generator` starts reading before candidate generation. Semantic review waits for that reading, then compares the candidate. Rewrites reuse the reading while material remains identical. Context reduction or material changes cause a new read.

Review timeout cancels the current wait. The ongoing reading remains available for the next attempt. Failed or invalid reads mark review unavailable and clear the cache. The next attempt reads again. Generation completion cancels and collects unused reading tasks.

`risk` mode reads only when remote review is necessary. Rewrites also reuse that reading.

`llm/media_evidence.py` gives generation and review the same image evidence. It distinguishes attachment presence from successful image reading. Missing-image replies use natural group speech about failed loading. They omit technical terms for reading or parsing an image. Agreement with a user's unseen-image judgment also counts as image confirmation.

### Unavailable reviewer policy

An unavailable review model returns `severity="infra"` and `failure_code="checker_unavailable"`:

- `always`: Explicit calls use `NOT_READY_FALLBACK`. Standard participation stays silent. Unreviewed candidates remain unsent.
- `risk`: Candidates that pass local rules can proceed. Persona questions receive `persona_grounding` rejection when low-risk persona improvisation is disabled.

### Rewrites and fallback: `reply_fallbacks.py`

| Condition | Action |
| --- | --- |
| Rewrite attempts remain | Build a targeted prompt from `failure_code`. Soft corrections change wording and tone. Hard corrections use visible evidence only. |
| Standard participation exhausts attempts | `persona_grounding`, `context_grounding`, and `instruction_following` produce silence. Other replanning cases raise `ReplyRejected` to PFC. Cases without replanning stay silent. |
| Explicit call exhausts attempts with soft issues only and local rules pass | Accept the candidate through `reply.check.exhausted.accept`. |
| Explicit call exhausts attempts with hard issues | Select a cause-specific fallback through `reply.check.exhausted.forced_fallback`. Skip repeated recent sentences. Finally use `PRESENCE_FALLBACK`. |

`ALL_FALLBACK_TEXTS` contains fallback sentences. Evaluation scripts use it to identify replies after review exhaustion.

## 🖼️ Multimodal pipeline

### Inbound

`media/event_media.py` validates sources and reconstructs `text`, `at`, `reply`, `face`, `mface`, and `image` in original order. `media/sources.py` reads event URLs, data URLs/base64, and authorized local paths. On failure, it calls `get_msg` or `get_image` through `onebot_media`. Multiple attachments match by file identifier.

`media/loading.py` bounds reads and decoding. `media/event_media_analysis.py` calls `vision`. Memes receive an additional background prompt. `media/render_cache.py` stores summaries by media hash. Prompt-version changes invalidate those summaries.

Media limits cover bytes, pixels, animation frames, timeout, retries, and inbox quotas. Memes enter a privacy-scoped `emoji_library`. Perceptual hashes merge similar images. `media_registry` keeps stable history references. After history truncation, image summaries remain available for follow-up questions.

### Outbound

`media/marker_resolver.py` resolves `[想发表情:…]`, `[想发QQ表情:…]`, and `[想发图片:…]`. It selects authorized assets from emoji libraries, image libraries, historical media, and QQ face catalogs. Unmatched markers are removed. `reply_payload.py` builds ordered batches. `reply_media.py` records asset use after delivery acknowledgment.

## 💾 State and persistence

### Process state: `runtime_state.py`

- Session statistics, locks, reply timestamps, consecutive counts, cooldowns, and current reply metadata.
- Name follow-up registrations lasting 60 seconds.
- Session and global model overrides.
- Media refinement, background learning, and debounced flush tasks.
- `GenerationLimiter`.

### Persistence

All stores inherit `store_base.py`. `store_binding.py` binds them to `context.data_dir`. Core atomic storage publishes files and keeps `.bak` backups.

Reads validate root objects, integers, finite floats, booleans, and collection elements. A corrupt primary file uses a valid backup. Unusable primary and backup files are quarantined. `io_persist_debounce_seconds` and `memory_db_save_debounce_seconds` control debounce.

| Rule | Implementation |
| --- | --- |
| Message numbering | First allocation reads the largest existing `mN`. This supports restart, idle reset, and cache eviction. |
| QQ message IDs | Delivery commit writes all IDs with character speech. `message_id` holds the first. `extra_message_ids` holds split-message IDs. Reply detection checks all recorded IDs. |
| Session locks | A lock cache and weak-reference registry keep the original lock for holders and waiters after cache eviction. |
| Reset generations | `.generation` marks profile directories. Reset updates it under a shared path lock, removes JSON/backups, and deletes vector records. Background tasks record the generation before remote calls and check it at commit. |
| Expressions and jargon | Loads include a baseline. Saves merge changes from that baseline into the latest disk state. |
| Emoji library | Collection/use counts commit under the index-path lock. Slow repairs analyze outside the lock, validate the old snapshot, and commit differences. |
| Quarantined memory | Chat memory queries exclude `quarantined_` types. |
| Conversation boundaries | At `conversation_idle_gap_seconds`, raw history stays. Goals, PFC, action history, recent recall, and topic summaries start a new segment. |

### Shutdown

`shutdown()` stops new tasks and flushes. It then cancels background tasks and waits up to 2 seconds. After all tasks end, it flushes again. Remaining tasks cause `TimeoutError`. Core records shutdown failure. All plugins share the Core shutdown budget.

## ⚙️ Configuration lifecycle

| Source | Content |
| --- | --- |
| `config/config.json` → `bot_name` | Character name |
| `config/config.json` → `plugins.xiaoqing_chat` | AI routes, model aliases, and optional behavior fields |
| `plugins/xiaoqing_chat/config/xiaoqing_config.json` | Behavior, rate limits, planning, memory, media, persona switches, and debugging. File values take priority when merged. |
| `plugins/xiaoqing_chat/config/persona.md` or `personality.persona_file` | Persona facts, speech style, reply examples, and mood |
| `config/secrets.json` | Provider API keys |

`chat_runtime.py` reads Core atomic snapshots and caches file reloads. Configuration or persona file changes cause reload. `config/config.py` validates ranges and cross-field constraints. Session/user concurrency stays within global concurrency. Planner timeout stays within `timeout_seconds`. Each turn uses its own snapshot.

Configuration loading reads the persona into `PersonalityConfig.card`. Unmarked facts go to planning, expression learning, and review as controlled profile data. Generation puts facts at the start of the system prompt. `〔聊天〕` style and examples go at the end, nearest to the conversation. Agent handoff supplies facts and `〔办事〕` sections only.

`persona.py` fills `{{name}}` from snapshot `bot_name`. The persona card uses that placeholder. Historical character messages use the `assistant` role and render with the current name.

## 🔐 Errors and logs

Module parsing, type conversion, and file operations catch specific exceptions. AI providers, dynamic capabilities, media decoding, background tasks, and lifecycle code form isolation boundaries. They log redacted error categories and return controlled fallback results.

`logging_utils.log_step` emits structured `xiaoqing_chat step=<步骤>` fields. Rules redact QQ numbers, group numbers, URLs, profiles, and free text.

| Step | Meaning |
| --- | --- |
| `smalltalk.recv` | Message receipt and invocation fields |
| `smalltalk.no_reply` | Gate rejection with reason, probability, and draw |
| `smalltalk.explicit_silence` / `smalltalk.ignore` | Stop-chat request / blocked word |
| `smalltalk.generation_limited` | Exhausted generation quota |
| `smalltalk.forced_direct` / `smalltalk.pfc.done` / `smalltalk.planner.disabled` | Three generation paths |
| `reply.check.start` / `result` / `regen` / `timeout` / `error` | Review processing |
| `reply.check.exhausted.accept` / `forced_fallback` / `proactive_silence` | Actions after rewrite exhaustion |

## 🛠️ Extension locations

| Behavior | Location |
| --- | --- |
| Invocation | `attention_gate.py` |
| Standard participation cues | `participation.py` |
| Rate limits and probability | `frequency_control.py` |
| Planning | `planning/` |
| Generation context | `context_builder.py`, `reply_plan.py`, `llm/prompt_builder.py` |
| Persona | `config/persona.md`, `persona.py`, and `PersonalityConfig` in `config/config.py` |
| Review rules | `llm/local_checks.py` and `llm/reply_checker.py` |
| Rejection handling | `reply_fallbacks.py` |
| Inbound media | `media/event_media.py`, `media/sources.py`, `media/event_media_analysis.py` |
| Outbound media | `media/marker_resolver.py`, `reply_payload.py` |
| Long-term memory and profiles | `memory/` |
| Expressions and jargon | `expression/` |
| Persistence | `store_base.py` |

Add unit tests for new domain entries. Add `handle_smalltalk()` tests or real-model scripts for cross-layer changes. Add corrupt-input, concurrency, and restart tests for persistence changes. See the [acceptance guide](xiaoqing_chat测试.en.md).
