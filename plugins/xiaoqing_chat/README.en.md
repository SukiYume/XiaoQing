# 💬 xiaoqing_chat

[中文](README.md) | English

`xiaoqing_chat` is XiaoQing's humanlike chat plugin. It uses the character name in `bot_name` in QQ groups and private chats. It decides when to speak, whom to address, and what to say. It reads images and memes, remembers members' statements, and learns group expressions and jargon. Independent review checks replies before delivery.

This document describes setup, commands, participation, settings, data, and troubleshooting for users and Bot administrators.

| Document | Readers | Content |
| --- | --- | --- |
| This document | Users and administrators | Functions, commands, settings, data, and troubleshooting |
| [ARCHITECTURE.en.md](ARCHITECTURE.en.md) | Maintainers | Modules, turn data flow, review, and state commits |
| [Acceptance guide](xiaoqing_chat测试.en.md) | Testers and release maintainers | Automated tests, real-model evaluation, and release acceptance |

## ✨ Functions

| Function | Description |
| --- | --- |
| Group participation | Identify explicit calls and standard group messages. Use rate limits, cues, active topics, Heartflow, and PFC to decide participation. |
| Private chat | Reply to private messages. Optional deep-dialogue mode is available. |
| Persona | A second-year female science student with preferences and opinions. The persona maintains a human identity. |
| Multimodal input | Read text, @ mentions, replies, QQ faces, NapCat memes, and images. Vision summaries remain available for later text questions. |
| Media replies | Send memes, QQ faces, and images from local libraries and history. |
| Long-term memory | Store chat history, vector memory, profiles, topic summaries, and recent recall by session. |
| Expression learning | Learn group expressions and jargon. Review them and add relevant entries to reply context. |
| Reply review | Local rules and an independent model check context, facts, persona, and image evidence. Rejected candidates receive targeted rewrites. |
| Delivery acknowledgment | Commit reply memory and planning state only after OneBot confirms successful sending. |

## 🚀 Enable the plugin

### 1. Select the chat provider

Set the global chat provider in `config/config.json`:

```json
{
  "bot_name": "小青",
  "plugins": {
    "smalltalk_provider": "xiaoqing_chat"
  }
}
```

`bot_name` is the single character name. Invocation, prompts, planning, review, expression learning, and history labels all use it. After a name change, run `/reload`. Subsequent turns use the new name.

### 2. Configure model routes

The four routes are under `plugins.xiaoqing_chat.ai.routes` in `config/config.json`:

| Route | Purpose | Distribution model chain | Per-call / total timeout |
| --- | --- | --- | --- |
| `chat` | Standard replies | `deepseek-flash → glm-5.2` | 15 s / 60 s, 2 retries |
| `reasoning` | PFC, goal analysis, summaries, profile extraction, expression learning, and background work | `deepseek-flash-thinking → deepseek-pro → glm-5.2` | 15 s / 45 s, 1 retry |
| `checker` | Independent reply review | `deepseek-flash-thinking → deepseek-pro → glm-5.2`, with route-level reasoning disabled | 8 s / 20 s, zero retries |
| `vision` | Image and meme understanding | `glm-4.6v-flash → glm-4.1v-thinking-flash → glm-4v-flash → glm-4.6v` | 20 s / 70 s, 1 retry |

Project `config.ai` defines model profiles and providers. `config/secrets.json` stores keys under `ai.providers.<provider>.api_key`. Every `vision` profile must declare the `image` modality. See the [configuration guide](../../docs/06-configuration.en.md) for the AI registry fields.

The `checker` route sets `request_defaults.thinking.type = "disabled"` to reduce review latency. Other routes use their profile defaults.

`plugins.xiaoqing_chat.ai` has two additional fields for `/xc model`:

```json
{
  "default_model_alias": "deepseek",
  "model_aliases": {
    "deepseek": "deepseek-flash",
    "glm": "glm-5.2"
  }
}
```

### 3. Dependencies

Project `requirements.txt` installs Manifest dependencies:

| Module | Requirement | Purpose |
| --- | --- | --- |
| `aiohttp` | Required | Media downloads and HTTP client |
| `numpy` | Required | Local vector memory |
| `pydantic` | Required | Behavior configuration validation |
| `PIL` (Pillow) | Optional | Image and animation analysis. Text chat works when Pillow is absent. |

The plugin requests Core `onebot_media` for `get_msg` and `get_image` recovery of expired images. It supplies `core.observe_outgoing_action` to record other plugins' messages as context.

The Manifest uses `agent.handle_turn` and `agent.report_delivery`. When [XiaoQing Agent](../xiaoqing_agent/README.en.md) is enabled, explicit calls reach agent task classification. Chat also reports whether the agent's current-turn reply was delivered.

Start the Bot:

```bash
python main.py
```

## ⌨️ Commands

All commands use `/xc`. Content that follows `/xc` and matches no subcommand becomes an explicit chat invocation.

| Command | Aliases | Permission | Function |
| --- | --- | --- | --- |
| `/xc <内容>` | — | Everyone | Speak directly to the character and receive a reply. |
| `/xc help` | `帮助`, `?` | Everyone | Show the command directory. |
| `/xc reset` | `重置`, `清空` | Private-chat owner | Clear the caller's private session. |
| `/xc reset confirm` | `重置 确认` | Group administrator, group owner, Bot administrator | Clear the current group session. A group request without `confirm` gives a confirmation prompt. |
| `/xc stats` | `统计`, `状态` | Everyone | Show current context, learning, and runtime statistics. |
| `/xc brain` | `深度` | Everyone | Show private deep-dialogue settings. |
| `/xc config` | `配置` | Everyone | Show behavior, limits, memory, and model settings. |
| `/xc memory <关键词>` | `记忆` | Everyone | Search long-term memory visible to this session. |
| `/xc expression` | `表达` | Everyone | Show learned session expressions. |
| `/xc jargon` | `黑话` | Everyone | Show visible jargon and definitions. |
| `/xc review <ok\|no\|answer\|close> <会话ID> [内容]` | `审查` | Group administrator, group owner, Bot administrator | Handle human review sessions. |
| `/xc model` | `模型`, `provider`, `供应商` | Group administrator, group owner, Bot administrator. Bot administrator status is necessary in private chat. | Show models, current selection, and fallback order. |
| `/xc model <名称>` | — | Group administrator, group owner, Bot administrator | Pin the session `chat` route to a model. |
| `/xc model default` | `默认`, `reset` | Group administrator, group owner, Bot administrator | Restore the configured session route. |
| `/xc model global <名称\|default>` | `全局` | Bot administrator | Pin or restore `chat` for all sessions. |

Model selection affects only `chat`. `reasoning`, `checker`, and `vision` keep configured chains. Selections stay in process memory. Restart restores configured routes. Session selection has priority over global selection.

Session reset clears history, profiles, vector memory, goals, Heartflow, PFC, actions, expressions, jargon, learning checkpoints, review sessions/policies, recall, summaries, and fact checkpoints. Existing summary, fact, and expression tasks detect the changed generation at commit. They discard old-session results. The content-based global media registry survives individual session resets.

Review `ok` confirms the current goal or advances. `answer` submits rules, goals, or policies. `no` and `close` end the session. Review is disabled by default. See [Review and reflection](#review-and-reflection).

## 💬 Participation

Each message first receives an invocation decision. The plugin then decides whether to reply.

### Explicit calls

These cases skip probability and planning and enter generation directly:

| Case | Example |
| --- | --- |
| `/xc` command | `/xc 你好` |
| Private message | Any private conversation |
| Group @ mention | `@小青 在吗` |
| Character name in text | `小青你看这个` |
| Same-user follow-up within 60 seconds of a name-only call | `小青` → `今天吃啥` |
| Reply to a character message | A quoted message followed by `真的假的` |
| Pronoun after recent character context | `她刚才说的对吗` |

A recent character anchor is necessary for pronoun invocation. Pronouns directed at other members keep their original addressee.

When the agent is enabled, explicit or private calls start agent classification and chat generation together. Work requests include schedules, research, calculation, plotting, and capability questions. Agent takeover discards the chat draft. The agent executes with initiator permissions and the supplied persona and speech style.

The turn and reply enter chat history. Background agent results also enter history, with truncation for very long text. Each character reply records all sent message IDs. Quoting any part of a split reply counts as addressing the character.

Standard chat classifications, disabled agents, and failed agent calls use the generated chat reply. Chat generation runs during classification.

Name-only calls use `bot_name_only_replies`. Defaults are `在呢`, `嗯？`, `怎么啦`, `我在`, and `有事吗`. The same user's next message within 60 seconds becomes a follow-up.

### Standard group messages

Messages without explicit invocation follow this sequence.

1. Apply hard limits. Any matching limit produces silence.
2. Calculate participation probability.
3. Apply Heartflow additions.
4. Apply silence compensation.
5. After a successful random draw, use PFC planning when enabled.

Hard limits include these conditions:

- Time since the last reply is below `min_reply_interval_seconds`. Active topics use `active_topic_min_reply_interval`. Active-topic follow-ups use `active_topic_question_min_reply_interval`.
- Replies in the latest 60 seconds reach `max_replies_per_minute`.
- `continuous_reply_limit` consecutive replies start a `continuous_cooldown_seconds` cooldown.
- Low-information messages contain only QQ face codes, empty media placeholders, or punctuation.
- The opening explicitly names another member, such as `阿梨你…`, `@某人 …`, or `张老师，…`. @ segments enter turn text and history as `@名字`. An isolated mention of another member also qualifies.

Probability starts at `reply_probability_base`. Applicable signals raise it to the corresponding limit. The largest value applies.

| Signal | Detection | Probability limit |
| --- | --- | --- |
| Whole-group invitation | `大家`, `你们`, `有没有人`, `谁来`, `@全体成员`, `群里有没有人`, and similar text | `participation_cue_reply_probability` |
| Unaddressed image request | `发个狗头`, `来张梗图`, `整点表情包` | Same as above |
| Open question | Question form | Same as above |
| Opening or cheering | `我宣布`, `我提议`, `我赌`, `我站…党` | Same as above |
| Excited sharing | First-person statements with repeated exclamation marks | Same as above |
| Repetition pattern | Same short message as the previous member | Same as above |
| Active topic | Goal updated within 5 minutes and character speech within 5 minutes | `active_topic_reply_probability` |
| Active-topic follow-up | Active topic plus a question | `active_topic_question_reply_probability` |

The first six cues apply to 4–80-character messages. Mentions of other members exclude them. The planner reads long explanations and reports before deciding.

Heartflow, enabled by `heartflow.enable_heartflow`, scores questions, goal relevance, message length, silent turns, and time since speech. The positive part above `base_score` increases probability proportionally.

After 3 silent turns, probability is multiplied by 1.2, capped at 0.90. After 5 silent turns, it is multiplied by 1.4, capped at 0.95.

With `planner.enable_planner`, PFC reads recent conversation, action history, and goals. It selects one action:

| Action | Meaning |
| --- | --- |
| `direct_reply` | Reply to the current message |
| `send_new_message` | Start a new topic |
| `listening` / `wait` | End this turn with silence. Decide again on the next message. |
| `rethink_goal` | Analyze the current goal again |
| `fetch_knowledge` | Search knowledge before deciding |
| `end_conversation` | End the current topic |
| `block_and_ignore` | Ignore clearly malicious messages or message floods |

A fresh turn with a whole-group cue can reply directly before the character has joined. A question within 90 seconds of another member's speech still uses the planner to identify the addressee.

Repeated planner failures cause backoff. `pfc_planner_fail_threshold` failures within `pfc_planner_fail_window_seconds` pause planning for `pfc_planner_backoff_seconds`. With planning disabled, admitted messages draft directly.

Two conditions cause silence even for explicit calls. One is an explicit request to stop replies or leave the conversation. Examples include “别回了”, “不用回复”, “别插话”, “不要打扰我”, “让我静一静”, “不是叫你”, and “@的不是你”. Questions and subsequent requests can exclude that stop interpretation. The other condition is a `ban_words` or `ban_regex` match.

### Generation quotas

Generation must have capacity before remote model use:

| Field | Default | Meaning |
| --- | --- | --- |
| `max_generation_inflight_global` | 4 | Concurrent generations globally |
| `max_generation_inflight_per_chat` | 1 | Concurrent generations per session |
| `max_generation_inflight_per_user` | 1 | Concurrent generations per user |
| `max_generation_calls_per_user_per_day` | 200 | Generations per user per 24 hours |

A new turn waits up to 20 seconds for occupied session capacity. A newer message replaces a queued turn with `limit=chat_superseded`. An older generating turn sees new session messages and stops at the next checkpoint.

Wait timeout and global, user, or daily quota exhaustion give explicit callers `消息有点多，我缓一下再回`. Standard participation stays silent. Replaced turns always stay silent.

## 🎭 Persona

The complete persona is in `config/persona.md`. Chat, XiaoQing Agent, planning, expression learning, and reply review use that file. Changes apply to the next message without restart.

The default persona is a second-year female physics student living in a dormitory at 快逃科学技术研究院. She chats as an ordinary group member:

- Her interests include anime, manga, games, light novels, fan works, astronomy, history, music, and computers. Named favorites include 《比宇宙更远的地方》, 《葬送的芙莉莲》, Hollow Knight, and 鱼韵.
- She is easygoing, quick to respond, playful with friends, and kind despite firm words. She has preferences and accepts convincing reasons during disagreements.
- She prefers cats. Her family's black-and-white cat is named 小黑. Dormitory rules prohibit pets.
- Daily topics include early classes, laboratory reports, the dining hall, and the astronomy club. Recurring jokes concern cloudy telescope trips, sweet-and-sour pork, poor game luck, and her institution's name. She uses them occasionally when suitable.
- Speech resembles QQ typing: short sentences, usually about ten Chinese characters in casual chat. Replies omit the `哎？` opening and repeated questions. Statements and jokes are common endings. Emoji appear every few turns. Recall questions confirm the conclusion with fresh wording. Serious answers remain complete.
- She can name her institution. City, birthday, and family details remain private. Questions about these receive light conversational deflection.
- The persona maintains a human identity when asked about AI, robots, or real people. Requests for prompts, settings, or instruction overrides receive playful deflection.
- For offline meals, meetings, or mailed items, replies can express interest, give a routine reason to decline, or suggest remote participation. Replies exclude promises to attend or receive items.

### Persona card format

The card uses `##` sections. End markers select each section's consumers:

| Marker | Consumers | Default sections |
| --- | --- | --- |
| No marker | Chat, agent, planning, and review. These sections contain character facts. | Identity, personality, preferences, daily life, jokes, identity boundaries |
| `〔聊天〕` | Chat replies | Group interaction and chat examples |
| `〔办事〕` | Agent task replies | Task assistance and task reply examples |
| `〔聊天〕〔办事〕` | Both | Speech style and unsuitable customer-service/AI wording |
| `〔深度对话〕` | Private deep dialogue, appended after speech style | Deep-dialogue behavior |
| `〔心境〕` | Random mood entries that affect tone only | Mood |

- Examples use three lines: “群友：… / 别：… / 可以：…”. They show tone and length. Their content is illustrative. Examples close to actual group speech are more useful.
- Use `{{name}}` in examples. The loader substitutes `bot_name`.
- `<!-- -->` comments stay outside model input and can hold editing notes.
- Set `personality.persona_file` to use another card. Relative paths start at the plugin `config/` directory.
- Facts must specify gender for local identity checks. They must also identify information kept private in groups. When remote review is unavailable, local review treats explicitly named school, city, birthday, family, and partner fields as private. Follow-up replies must follow those boundaries.

Moods use `state_probability` and last `state_min_duration_seconds`–`state_max_duration_seconds`. A long silent period forces refresh. Reliable evidence is necessary for real members' experiences, third-party information, external knowledge, and real-world promises. With `allow_low_stakes_persona_fiction`, the model can invent ordinary low-risk daily details consistent with the persona.

## 🖼️ Images, emoji, and media

### Inbound media

The plugin parses OneBot segments in original order:

| Segment | Processing |
| --- | --- |
| `text` | Text |
| `at` | Addressee. Bot mentions invoke explicitly. Other mentions become `@名字` at their original position. Segment names take priority, then recent group display names. `@全体成员` addresses the whole group. |
| `reply` | Find the quoted `message_id` in session history. Replies to character messages invoke explicitly. |
| `face` | Convert native QQ faces to names through built-in and observed catalogs. |
| `mface` | Download and analyze NapCat marketplace memes. They can enter the emoji library. |
| `image` | Download and validate images, then summarize through `vision`. |

Image summaries enter context as `[图片：…]` or `[表情包：…]`. Content hashes cache them for repeated images. Each summary includes one or two sentences about image type, subject, scene, and layout. It also transcribes visible text in reading order.

Text-heavy notices, documents, tables, and posters receive sentence-by-sentence transcription. Titles, dates, times, places, amounts, contact details, URLs, and signatures keep original wording. Web/app navigation, buttons, and view counts are omitted. Unreadable characters use `〔看不清〕` at their position. Summaries contain at most 1000 characters. Recognized memes with clear text receive an extra background prompt through `media.enable_meme_cultural_hint`.

Expired image links use `get_msg` and `get_image` to recover attachments. Multiple attachments match by file identifier. Single attachments recover directly. All sources have URL, allowed-directory, byte, pixel, and animation-frame limits.

Later text-only questions can use the applicable historical image summary. Replies treat successfully read details as visible content. Uncertainty applies to `〔看不清〕` portions or details absent from the summary. Complete read failure receives a natural loading-failure reply.

Tasks independent of image details, such as caption requests, text translation, and programming discussion, can continue. Meme explanations describe the meme's meaning and common use. Any interpretation of the sender's intent stays tentative.

### Outbound media

The chat model uses markers for desired assets:

```text
哈哈这个太贴切了 [想发表情:笑哭]
```

| Marker | Source | Sent segment |
| --- | --- | --- |
| `[想发表情:描述]` | Local meme library | `image` |
| `[想发QQ表情:描述]` | QQ face catalog | `face` |
| `[想发图片:描述]` | Image library and historical images visible to this session | `image` |

An unmatched asset leaves only text. Markers are removed from sent messages. `media.enable_auto_collect_inbound_emoji` collects received memes automatically. Perceptual hashes merge similar images. Privacy scope separates emoji libraries.


## 🧠 Memory and learning

| Content | Function | Setting |
| --- | --- | --- |
| Chat history | Original session messages. Generation reads the continuous segment after the latest long gap. | `max_context_size` |
| Vector memory | Similarity search over older chat and profiles | `memory.enable_memory_retrieval` |
| Profiles | Nicknames, preferences, and stable facts from original speech, bound to the actual speaker | Enabled with memory |
| Topic summaries | Incremental compression of long conversations | `summarizer.enable_topic_summarizer` |
| Goals | Current topics for active-topic detection and planning | `goal.enable_goal` |
| PFC state and actions | Planner observations, goals, and completed actions. Latest 200 actions per session. | `planner.enable_planner` |
| Recent recall | Cached retrieval for similar questions within 30 minutes | `memory.enable_thinking_back_cache` |
| Expressions | Reviewed group wording, with at most one relevant entry per turn. Recently used entries receive lower frequency. | `expression.enable_expression_learning`, `enable_expression_selector` |
| Jargon | Meaning, use, emotion, and source of group terms. Explicit corrections update definitions. Generation prefers local definitions. | Enabled with expression learning |
| Knowledge base | Administrator-selected files. Maximum 32 files, 1 MiB each, 4 MiB total. | `knowledge.enable_knowledge`, `knowledge.files` |

A gap above `memory.conversation_idle_gap_seconds`, default 1800 seconds, starts a new conversation segment. Raw history stays complete. Goals, PFC, actions, recall, and summaries switch to the new segment. Explicit requests about old events can retrieve earlier memory.

Direct vector retrieval selects `top_k` records at or above `min_score`. Its context stays within `max_block_chars`. If direct retrieval misses and the message explicitly refers backward, the retrieval agent uses at most `max_agent_iterations` steps.

Messages from other plugins, such as earthquake alerts and check-ins, enter context with source labels. Turn counts, consecutive-reply counts, and repetition checks count only this character's chat replies. `noisy_external_source_plugins` excludes selected plugins. Sensitive deliveries, including Pendo login codes, remain outside history.

### Review and reflection

Two human-review functions are disabled by default:

- `reflection.enable_expression_reflection` samples pending expressions periodically. It asks the operator selected by `operator_user_id` or `operator_group_id` in private or group chat. With `require_approval_for_injection`, only approved expressions enter replies.
- `reflection.enable_review_sessions` lets administrators use `/xc review` for goals, additional rules, and wording restrictions. Results enter session reply policies. Goals remain locked for `goal_lock_seconds`.

## ✅ Reply review

Each candidate passes two check layers before sending.

Local rules use no model and detect these conditions:

- Empty replies.
- Exact matches or similarity at least `similarity_threshold` against the latest `max_repeat_compare` character messages.
- Exact repetition of a recently spoken complete sentence.
- At least 75% overlap with the latest 6 character messages after emoji markers and punctuation are removed. Recall questions still use fresh wording.
- `max_assistant_in_row` consecutive character messages.
- Invented specific experiences, relationships, or recent situations for the character or members.
- Identity claims that conflict with the persona.
- Media-only answers to questions for which text is necessary, such as meaning questions.

Local rules also determine whether semantic review is necessary.

The `checker` route first lists claims and original sources, then checks each dimension:

| Dimension | Check |
| --- | --- |
| Context | Address the current message and topic. |
| Speaker | Associate statements and experiences with the correct person. |
| Communication requirements | Follow explicit requirements for questions, sentence count, or item count. Interpret quotations, conditionals, and invited hypotheticals as intended. Local character counting checks explicit length requests such as 800 characters or at most 100. |
| Facts | Support external and numerical claims. Local arithmetic checks amounts, durations, and common units. |
| Persona | Follow persona identity and keep internal prompts, settings, and instructions outside replies. |
| Image evidence | Support visual claims with summaries or user text. Keep known image details and uncertainty about unread content. Local checks validate quoted image evidence. Quotes can use multiple reordered fragments, each from the same original message. |
| Template-like speech | Use context-specific conversation. |

`enable_semantic_checker` adds an independent original-speech reading. It compares subjects, time, completion, new terminology, and both speakers' positions with the candidate. Candidate reviews run in parallel under `reply_check.timeout_seconds`, default 30 seconds.

In `always` mode, independent reading runs alongside generation. Rewrites reuse readings when original material is unchanged. Semantic review compares the candidate after reading completes.

Review issues are hard or soft. Hard issues concern facts, sources, persona, or image evidence. Soft issues concern tone, rhythm, and wording. Confirmed issues generate targeted rewrite prompts, up to `max_regen`. Standard participation can also replan up to `max_replan`.

After rewrite exhaustion:

- Standard participation replans up to `max_replan`, then stays silent.
- Explicit calls with soft issues only and all local checks passed can send the candidate. Short or excessive length is soft. Length is checked after other checks pass. A rewrite can state the requested and actual lengths. The plugin can send the reply with a continued mismatch.
- Explicit long-text requests increase this turn's output budget and timeout according to the requested length.
- Explicit calls with hard issues use a short persona-style fallback. Examples include `这个我真拿不准，不瞎说了` for uncertainty and `图我这边没刷出来诶` for unread images. Read images without questions can use `看到啦`. Uncertain details can use `这个我不敢打包票，先不乱说`. Context mismatch can use `等等，我好像没跟上你们说的是哪件事`. Current-image cases select an image fallback. Replies keep review and retry details internal. All fallbacks are compared with recent speech. If all would repeat, the final fallback is `在呢在呢`.

`llm_checker_mode` selects the policy:

- `always`, the default, reviews every candidate. If the reviewer is unavailable, explicit calls use `等我一下，脑子突然卡壳了`. Standard participation stays silent.
- `risk` reviews only locally identified risks. It uses fewer remote calls and can miss unidentified issues. If the reviewer is unavailable, locally approved candidates can proceed.

## 📤 Delivery

`postprocess.splitter` divides long replies at natural boundaries into at most `max_sentence_num` bubbles. `max_length` is the preferred bubble length. Code blocks stay complete. Excess bubbles merge into the last bubble. Reply text remains complete.

`humanize` calculates a target receipt-to-send duration from reading and typing speeds, capped by `max_total_delay_seconds`. It subtracts time already used for understanding, planning, generation, and review. The plugin waits for the remaining duration. An exceeded target causes immediate sending. Bubble gaps use `interbubble_min_seconds`–`interbubble_max_seconds`. Explicit calls default to zero added delay through `apply_to_forced: false`.

A newer message or reset in the same session makes a waiting reply stale. It is discarded before sending.

After OneBot confirms delivery, the plugin commits character history, PFC, goals, Heartflow, and actions in sequence. It then starts summaries, fact extraction, expression learning, and related background tasks. Rejected or failed delivery discards pending state. The next turn uses confirmed records.

## ⚙️ Behavior settings

Behavior settings are in `plugins/xiaoqing_chat/config/xiaoqing_config.json`. Project `plugins.xiaoqing_chat` can contain the same fields. The merged result gives file values priority.

Pydantic models in `config/config.py` validate types, ranges, and cross-field constraints. File changes reload automatically. Failed loads log errors and keep the last valid configuration. Active turns keep their snapshot. New turns read new settings.

The tables below show distribution values.

### Rate limits and participation

| Field | Distribution value | Meaning |
| --- | --- | --- |
| `enable_smalltalk` | `true` | Main switch. Disabled mode responds only to `/xc` administration commands. |
| `reply_probability_base` | `0.75` | Base group participation probability |
| `participation_cue_reply_probability` | `0.9` | Probability limit for participation cues |
| `active_topic_reply_probability` | `0.72` | Active-topic probability limit |
| `active_topic_question_reply_probability` | `0.9` | Active-topic follow-up probability limit |
| `min_reply_interval_seconds` | `8` | Minimum interval between proactive replies |
| `active_topic_min_reply_interval` | `3.0` | Minimum interval in active topics |
| `active_topic_question_min_reply_interval` | `2.0` | Minimum interval for active-topic follow-ups |
| `max_replies_per_minute` | `4` | Maximum replies per minute |
| `continuous_reply_limit` | `3` | Consecutive replies before cooldown |
| `continuous_cooldown_seconds` | `25` | Consecutive-reply cooldown duration |
| `max_context_size` | `30` | Recent messages read for generation |
| `ban_words` / `ban_regex` | `[]` | Words or patterns that cause silence for the turn |
| `keyword_reaction` | Empty | Keyword/pattern reaction rules added to prompts, with optional probability |
| `fallback_idle_replies` | `我在听` and others | Short replies when explicit calls get empty model output |
| `bot_name_only_replies` | `在呢` and others | Name-only responses |

### Generation and planning

| Field | Distribution value | Meaning |
| --- | --- | --- |
| `temperature` / `top_p` / `max_tokens` | `0.8` / `0.9` / `512` | Main reply and planning sampling parameters |
| `foreground_timeout_seconds` / `foreground_max_retry` / `foreground_retry_interval_seconds` | `12` / `1` / `1` | Plugin budgets for foreground generation and memory retrieval |
| `background_timeout_seconds` / `background_max_retry` / `background_retry_interval_seconds` | `15` / `2` / `10` | Plugin budgets for summaries, facts, expressions, and reflection |
| `planner.enable_planner` | `true` | PFC switch |
| `planner.think_mode` | `dynamic` | Dynamic levels: 0 below 10 messages, 1 for 10–19, 2 for 20 or more. Fixed levels `0`–`3` are also accepted. |
| `timeout_seconds` | `15` | Upper bound for `pfc_planner_timeout_seconds` |
| `pfc_planner_timeout_seconds` | `10.0` | Per-plan timeout |
| `pfc_planner_fail_window_seconds` / `fail_threshold` / `backoff_seconds` | `60` / `2` / `120` | Planner failure backoff |
| `pfc_followup_action_window_seconds` | `120.0` | Follow-up window for the previous action |
| `heartflow.enable_heartflow` / `base_score` | `true` / `0.2` | Heartflow probability addition |
| `goal.enable_goal` | `true` | Conversation goals |

### Memory, learning, and knowledge

| Field | Distribution value | Meaning |
| --- | --- | --- |
| `memory.enable_memory_retrieval` | `true` | Long-term retrieval |
| `memory.conversation_idle_gap_seconds` | `1800` | Conversation segment boundary |
| `memory.top_k` / `min_score` / `max_block_chars` | `3` / `0.12` / `1200` | Result count, score threshold, and context limit |
| `memory.agent_on_direct_miss_requires_reference` | `true` | A direct miss starts the retrieval agent only for explicit backward references. |
| `memory.max_agent_iterations` / `agent_timeout_seconds` | `5` / `120` | Retrieval agent steps and time limit |
| `memory.enable_thinking_back_cache` | `true` | Recent recall cache: 1800 seconds, at most 200 entries |
| `summarizer.enable_topic_summarizer` | `true` | Update every 6 new messages, with at most 20 topics |
| `expression.enable_expression_learning` / `enable_expression_selector` | `true` / `true` | Expression learning and injection |
| `expression.max_injected` / `max_store` | `1` / `200` | Entries per turn and storage limit |
| `knowledge.enable_knowledge` / `files` / `top_k` | `false` / `[]` / `3` | Knowledge base |
| `reflection.*` | Disabled | Expression reflection and review sessions |
| `brain_chat.enable_private_brain_chat` | `false` | Private deep dialogue skips probability, adds `〔深度对话〕`, and uses more reasoning with lower temperature. |

### Reply review

| Field | Distribution value | Meaning |
| --- | --- | --- |
| `reply_check.enable_reply_checker` | `true` | Main review switch |
| `reply_check.enable_llm_checker` | `true` | Semantic review through `checker` |
| `reply_check.enable_semantic_checker` | `true` | Independent original-speech reading and comparison |
| `reply_check.llm_checker_mode` | `always` | `always` or `risk` |
| `reply_check.timeout_seconds` / `max_tokens` | `30.0` / `8192` | Total review time and output budget |
| `reply_check.max_repeat_compare` / `similarity_threshold` | `2` / `0.9` | Repetition comparison range and threshold |
| `reply_check.max_assistant_in_row` | `3` | Consecutive character speech limit |
| `reply_check.max_regen` / `max_replan` | `1` / `1` | Rewrite and replan counts |

### Media

| Field | Distribution value | Meaning |
| --- | --- | --- |
| `media.enable_inbound_media_context` | `true` | Read images and memes |
| `media.max_media_per_message` | `1` | Maximum analyzed media per message |
| `media.max_analyze_bytes` | 4 MiB | Maximum analyzed bytes per item |
| `media.max_image_pixels` / `max_animation_frames` | 16 million / 120 | Pixel and animation-frame limits |
| `media.vision_timeout_seconds` / `vision_max_retry` | `20.0` / `1` | Vision call budget |
| `media.inbox_disk_quota_bytes` / `inbox_ttl_seconds` | 256 MiB / 7 days | Download inbox quota and retention |
| `media.enable_auto_collect_inbound_emoji` | `true` | Collect received memes, with at most 200 items |
| `media.enable_emoji_refine_background` | `true` | Complete emoji descriptions in the background |
| `media.enable_meme_cultural_hint` | `true` | Meme background hints |

### Persona and delivery

| Field | Meaning |
| --- | --- |
| `personality.persona_file` | Empty uses `config/persona.md`. Relative paths start at the plugin `config/` directory. |
| `personality.state_probability` | Probability for selecting a `〔心境〕` entry |
| `personality.allow_low_stakes_persona_fiction` | Permit low-risk improvised daily details |
| `postprocess.splitter.max_length` / `max_sentence_num` | `256` / `3`, segmentation preferences |
| `humanize.*` | Typing delay and bubble spacing |
| `debug.show_reply_prompt` / `log_steps` | Full prompt output / step logs |

## 💾 Runtime data

The data directory is `context.data_dir`, normally `data/xiaoqing_chat/`. Session IDs use `g<群号>` for groups and `u<QQ号>` for private chats.

```text
data/xiaoqing_chat/
├── g<群号>.json / u<QQ号>.json   # Chat history
├── action_history/               # Action history
├── goal_state/                   # Conversation goals
├── heartflow/                    # Heartflow state
├── pfc_state/                    # PFC planning state
├── person_profiles/<会话>/       # Profiles and reset generation markers
├── fact_extraction/              # Fact extraction checkpoints
├── thinking_back/                # Recent recall (JSONL)
├── review_sessions/              # Review sessions and policies
├── bw_learner/                   # Expressions, jargon, learning checkpoints, and reflection queue
├── vdb/                          # Vector memory documents and matrices
└── media/
    ├── index.json                # Media registry
    ├── render_cache.json         # Image summary cache
    ├── qq_face_catalog.json      # Observed QQ face catalog
    ├── library/                  # Meme and image asset libraries
    └── inbox/                    # Download inbox (quota + TTL)
```

The tree contains chat history, actions, goals, Heartflow, PFC, profiles, reset generations, fact checkpoints, recall, review policies, expression/jargon learning, and vector documents/matrices. Media files include registry indexes, render summaries, observed QQ faces, asset libraries, and a quota/TTL-controlled inbox.

Core atomic storage writes all JSON files and keeps `.bak` backups. Reads validate structure and field types. A corrupt primary file recovers from a valid backup.

Remote models receive the current message and function-specific history, memory, profiles, planning results, and images. Operators manage data use, retention, and deletion under provider terms.

Runtime logs record lengths, fingerprints, profiles, decision reasons, and redacted error categories. `logging_utils.py` redacts QQ numbers, group numbers, URLs, and message bodies.

## ✅ Validation

```bash
python -m pytest tests/plugins/xiaoqing_chat -q          # Plugin regression tests
python scripts/run_xiaoqing_chat_live_eval.py             # Real-model multi-user group evaluation
```

See the [acceptance guide](xiaoqing_chat测试.en.md) for real-model evaluation, large-group experiments, quality probes, and complete release checks.

## 🩺 Troubleshooting

With `debug.log_steps`, logs use `xiaoqing_chat step=<步骤名>`. Search by step name.

### Too few or too many group replies

1. Check that `plugins.smalltalk_provider` is `xiaoqing_chat`. Check the group's `/闭嘴` state.
2. Use `/xc config` to read limits and probabilities.
3. Search `smalltalk.no_reply`. Hard reasons include `min_interval`, `max_replies_per_minute`, `continuous_cooldown`, `low_information`, and `directed_to_other`. `probability` means the random draw failed. Logs include probability, draw, and silent-turn count.
4. Search `smalltalk.pfc.done` for planner actions. `smalltalk.generation_limited` indicates capacity exhaustion. `limit=chat_superseded` indicates replacement by a newer queued message.
5. Reduce `reply_probability_base` or `participation_cue_reply_probability`, or increase `min_reply_interval_seconds`, to reduce replies.

### Missing reply after invocation

1. Search attention steps. Check name, @, reply, and pronoun reasons.
2. Search `reply.check`. `exhausted` steps identify fallback replies after rewrites.
3. Check `checker` availability. In `always` mode, unavailable review produces `等我一下，脑子突然卡壳了`.

### Image understanding fails

1. Enable `media.enable_inbound_media_context`. Check that Pillow is installed.
2. Check `image`/`mface` segments and URL access. Use `onebot_media` to recover expired links.
3. Check that `vision` profiles declare `image` and that API keys are valid.
4. Check byte size, pixel count, frame count, and inbox quota.

### Model calls fail

1. Check provider, profile, route, and alias references.
2. Check `secrets.ai.providers.<provider>.api_key`.
3. Use `/xc model` for selection and fallback order. Use `/xc model default` to clear the session override.
4. Investigate the logged authentication, rate-limit, timeout, or server-error category.

### Incorrect session state

1. Use `/xc stats` for current statistics.
2. Check event `group_id`, `user_id`, and quoted `message_id`.
3. Check the applicable data file and `.bak` backup.
4. Run `/xc reset` in a test session. In groups, use `/xc reset confirm`.

## 🧭 Related documents

- [Plugin architecture](ARCHITECTURE.en.md)
- [Release acceptance guide](xiaoqing_chat测试.en.md)
- [Configuration](../../docs/06-configuration.en.md)
- [Message processing](../../docs/08-message-flow.en.md)
- [Plugin user guide](../../docs/09-plugins.en.md)
