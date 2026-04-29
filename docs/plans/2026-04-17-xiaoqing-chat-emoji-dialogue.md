# XiaoQing Chat Emoji Dialogue Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make `plugins/xiaoqing_chat` understand user-sent images and memes, and optionally reply with a locally selected meme image, without importing MaiBot's full plugin/action system.

**Architecture:** Keep `xiaoqing_chat` text-first. Add a small xiaoqing-local media pipeline that renders incoming OneBot image segments into prompt-safe text markers such as `[图片：...]` and `[表情包：...]`, then run a separate optional emoji-reply planner after the normal text reply is finalized. Persist only rendered markers into memory and action history so existing planner, reply checker, and memory code can stay mostly unchanged.

**Tech Stack:** Python, aiohttp, Pillow, existing OpenAI-compatible chat client, OneBot message segments, local JSON caches, pytest.

---

## Current Comparison

- `xiaoqing_chat` currently loses all non-text content before the plugin sees it. `core/message.py` only extracts `type == "text"` segments, and `core/dispatcher.py` drops the whole event when the extracted text is empty.
- `xiaoqing_chat` is pure text end-to-end. `plugins/xiaoqing_chat/handlers.py` records and returns plain strings, and the final send path is always `segments(reply)` or split text messages.
- `xiaoqing_chat` prompt and rewrite rules are intentionally hostile to inline markup-like output. `plugins/xiaoqing_chat/config/config.py` and `plugins/xiaoqing_chat/llm/rewrite.py` both bias toward short plain chat text and explicitly discourage emoji-like formatting.
- MaiBot handles inbound media before planning. `../MaiBot/src/chat/message_receive/message.py` converts incoming `image` and `emoji` segments into textual placeholders, while `../MaiBot/src/chat/utils/utils_image.py` provides cached image/emoji understanding.
- MaiBot keeps media usable inside prompt construction. `../MaiBot/src/chat/replyer/group_generator.py` and `../MaiBot/src/chat/replyer/private_generator.py` analyze `[picid:...]`, replace them with image descriptions, and build dedicated "对方发送了图片" prompt blocks.
- MaiBot sends memes through a separate action, not by asking the main reply model to emit image markers. `../MaiBot/src/plugins/built_in/emoji_plugin/emoji.py` chooses an emotion label, then `../MaiBot/src/chat/emoji_system/emoji_manager.py` selects the actual file.

## Non-Goals

- Do not port MaiBot's full `plugin_system`, `ReplySetModel`, `send_api`, or WebUI.
- Do not depend on MaiBot runtime databases or `data/emoji_registed` as a hard dependency.
- Do not add emoji management commands in phase 1 unless they are required for smoke testing.
- Do not change `xiaoqing_chat` into a generic multimodal agent; keep the feature scoped to dialogue plus optional meme reply.

### Task 1: Let Image-Bearing Events Reach `xiaoqing_chat`

**Files:**
- Modify: `C:/Users/testuser/Desktop/VibeSpace/XiaoQing/core/message.py`
- Modify: `C:/Users/testuser/Desktop/VibeSpace/XiaoQing/core/dispatcher.py`
- Test: `C:/Users/testuser/Desktop/VibeSpace/XiaoQing/tests/test_message.py`
- Test: `C:/Users/testuser/Desktop/VibeSpace/XiaoQing/tests/test_dispatcher.py`

**Step 1: Add media-presence detection**

Add a helper in `core/message.py` that can tell whether a message contains meaningful non-text segments such as `image`, even when `extract_text()` returns an empty string.

**Step 2: Stop dropping image-only events**

Update `MessageParser.parse()` in `core/dispatcher.py` so that:
- text-only behavior stays unchanged
- command routing still uses `clean_text`
- image-only events are allowed through with empty `clean_text`, so the smalltalk provider can inspect raw segments later

**Step 3: Add parser tests**

Cover:
- image-only group messages are no longer dropped
- text-plus-image messages still preserve the original text routing behavior
- empty events with neither text nor media are still dropped

**Step 4: Run the focused tests**

Run:

```powershell
pytest tests/test_message.py tests/test_dispatcher.py -q
```

Expected:
- before implementation: new media cases fail
- after implementation: PASS

### Task 2: Build a Standalone `xiaoqing_chat` Media Pipeline

**Files:**
- Create: `C:/Users/testuser/Desktop/VibeSpace/XiaoQing/plugins/xiaoqing_chat/media/__init__.py`
- Create: `C:/Users/testuser/Desktop/VibeSpace/XiaoQing/plugins/xiaoqing_chat/media/event_media.py`
- Create: `C:/Users/testuser/Desktop/VibeSpace/XiaoQing/plugins/xiaoqing_chat/media/emoji_library.py`
- Modify: `C:/Users/testuser/Desktop/VibeSpace/XiaoQing/plugins/xiaoqing_chat/helper_utils.py`
- Modify: `C:/Users/testuser/Desktop/VibeSpace/XiaoQing/plugins/xiaoqing_chat/config/config.py`
- Modify: `C:/Users/testuser/Desktop/VibeSpace/XiaoQing/plugins/xiaoqing_chat/config/xiaoqing_config.json`
- Test: `C:/Users/testuser/Desktop/VibeSpace/XiaoQing/tests/plugins/test_xiaoqing_chat_media.py`

**Step 1: Add media config**

Introduce a small config block for media behavior, for example:
- `enable_inbound_media_context`
- `enable_outbound_emoji_reply`
- fixed emoji library path under `data/media/library`
- `emoji_reply_probability`
- `emoji_candidate_count`
- `emoji_cooldown_turns`
- `vision_provider` override or `vision.default` / `vision.providers` secrets config

The default should keep the feature disabled unless configured.

**Step 2: Implement incoming media resolution**

In `event_media.py`, add helpers that:
- walk raw OneBot message segments
- resolve `image` segment payloads from file URI / local path / URL into bytes
- compute a stable hash
- cache downloaded or copied files under the plugin data dir

Do not use MaiBot's base64-only assumptions directly; adapt the logic to XiaoQing's OneBot event format.

**Step 3: Implement image and meme rendering**

Add a renderer that turns incoming media into stable text markers:
- ordinary image: `[图片：一只猫躺在桌上]`
- meme/sticker: `[表情包：无语,阴阳怪气]`

Use a two-stage analysis inspired by MaiBot:
1. vision description
2. short emotion/tag extraction

Cache the final render result by image hash in plugin-local JSON so repeated images do not trigger repeated model calls.

**Step 4: Implement a standalone emoji library**

In `emoji_library.py`, scan a configured local directory and build a lightweight index with fields such as:
- `hash`
- `file_path`
- `description`
- `emotion_tags`
- `usage_count`
- `last_used_ts`

If metadata is missing, lazily generate it with the same rendering pipeline and persist it in a plugin-local JSON index.

**Step 5: Add focused unit tests**

Cover:
- URL/file-path image resolution
- cache hit vs cache miss
- emoji library indexing and metadata reuse
- safe behavior when the library is empty or media analysis is disabled

**Step 6: Run the focused tests**

Run:

```powershell
pytest tests/plugins/test_xiaoqing_chat_media.py -q
```

Expected:
- before implementation: FAIL
- after implementation: PASS

### Task 3: Feed Rendered Media Into Prompt, Memory, and Frequency Logic

**Files:**
- Modify: `C:/Users/testuser/Desktop/VibeSpace/XiaoQing/plugins/xiaoqing_chat/handlers.py`
- Modify: `C:/Users/testuser/Desktop/VibeSpace/XiaoQing/plugins/xiaoqing_chat/llm/prompt_builder.py`
- Modify: `C:/Users/testuser/Desktop/VibeSpace/XiaoQing/plugins/xiaoqing_chat/frequency_control.py`
- Test: `C:/Users/testuser/Desktop/VibeSpace/XiaoQing/tests/plugins/test_xiaoqing_chat.py`
- Test: `C:/Users/testuser/Desktop/VibeSpace/XiaoQing/tests/plugins/test_xiaoqing_chat_media.py`

**Step 1: Replace bare `clean_text` as the dialogue source**

In `handlers.py`, derive an `effective_user_text` from raw event segments:
- if the event is text-only, keep current behavior
- if the event contains media, append rendered media markers to the text
- if the event is image-only, use the rendered media text instead of dropping the turn

**Step 2: Persist rendered media markers into history**

Store the rendered string in `MemoryStore`, not raw URLs or file paths. This keeps history readable and lets the existing planner work without schema changes.

**Step 3: Keep prompt formatting marker-safe**

Ensure `build_dialogue_prompt()` leaves `[图片：...]` and `[表情包：...]` markers intact in conversation history so the main reply prompt can reason about them.

**Step 4: Teach reply gating that media-only turns are meaningful**

Update `_score_interest()` so image-only or meme-only turns are not automatically treated as empty or low-value noise.

**Step 5: Add regression tests**

Cover:
- image-only message enters `xiaoqing_chat` memory
- text-plus-image message is rendered into prompt history
- media-only turns can trigger a reply path
- existing text-only behavior remains unchanged

**Step 6: Run the focused tests**

Run:

```powershell
pytest tests/plugins/test_xiaoqing_chat.py tests/plugins/test_xiaoqing_chat_media.py -q
```

Expected: PASS

### Task 4: Add a Separate Emoji Reply Planner and Sender

**Files:**
- Create: `C:/Users/testuser/Desktop/VibeSpace/XiaoQing/plugins/xiaoqing_chat/reply_payload.py`
- Create: `C:/Users/testuser/Desktop/VibeSpace/XiaoQing/plugins/xiaoqing_chat/media/emoji_reply.py`
- Modify: `C:/Users/testuser/Desktop/VibeSpace/XiaoQing/plugins/xiaoqing_chat/handlers.py`
- Test: `C:/Users/testuser/Desktop/VibeSpace/XiaoQing/tests/plugins/test_xiaoqing_chat_media.py`

**Step 1: Add a reply payload abstraction**

Introduce a small internal structure that separates:
- `display_text` for memory/action history
- `outbound_batches` for actual OneBot sends

This avoids forcing image markers through the current text rewrite/postprocess path.

**Step 2: Keep the main text reply generator unchanged**

Continue using `_generate_reply()` for the human-like text reply. After the text reply is finalized and passes existing checks, run a second-stage optional emoji planner.

**Step 3: Implement the emoji planner**

In `emoji_reply.py`, implement:
- hard gates: feature enabled, emoji library not empty, no recent assistant meme spam
- candidate sampling from the local emoji library
- a tiny chooser prompt that picks one emotion/tag or `"none"` based on:
  - current user turn
  - final text reply
  - recent conversation
  - planner reason

Prefer the MaiBot pattern here: let the chooser pick an emotion, then let the library select the actual file.

**Step 4: Send emoji as a separate media step**

Build the final outbound sequence as:
- text only, or
- text then image

Phase 1 should avoid image-only replies except as an explicit later extension. That keeps the interaction closer to `xiaoqing_chat`'s existing style.

**Step 5: Record emoji sends in memory and history**

Persist a readable assistant-side marker such as:

```text
懂了
[表情包：无语]
```

This keeps later context coherent without changing `MemoryStore` schema.

**Step 6: Add tests**

Cover:
- no emoji is sent when the library is empty
- no emoji is sent during cooldown
- a valid emoji choice produces a `context.send_action()` image send
- the stored assistant memory contains the readable marker

**Step 7: Run the focused tests**

Run:

```powershell
pytest tests/plugins/test_xiaoqing_chat_media.py tests/plugins/test_xiaoqing_chat.py -q
```

Expected: PASS

### Task 5: Document, Verify, and Keep Scope Tight

**Files:**
- Modify: `C:/Users/testuser/Desktop/VibeSpace/XiaoQing/docs/06-configuration.md`
- Modify: `C:/Users/testuser/Desktop/VibeSpace/XiaoQing/docs/08-message-flow.md`
- Modify: `C:/Users/testuser/Desktop/VibeSpace/XiaoQing/plugins/xiaoqing_chat/plugin.json`
- Test: `C:/Users/testuser/Desktop/VibeSpace/XiaoQing/tests/test_message.py`
- Test: `C:/Users/testuser/Desktop/VibeSpace/XiaoQing/tests/test_dispatcher.py`
- Test: `C:/Users/testuser/Desktop/VibeSpace/XiaoQing/tests/plugins/test_xiaoqing_chat.py`
- Test: `C:/Users/testuser/Desktop/VibeSpace/XiaoQing/tests/plugins/test_xiaoqing_chat_media.py`

**Step 1: Document the config and emoji library layout**

Document:
- how to enable media context
- the fixed `data/media/library` emoji library layout
- how cache/index files are stored under the plugin data dir
- that phase 1 does not depend on MaiBot's DB or action framework

**Step 2: Update message-flow docs**

Revise the current docs that say non-text segments are ignored. After this feature, that statement should remain true for command routing but not for the `xiaoqing_chat` smalltalk path.

**Step 3: Add or update plugin help text**

Keep it concise. Mention that `xiaoqing_chat` can now understand image/meme messages when the media feature is enabled.

**Step 4: Run the full targeted regression slice**

Run:

```powershell
pytest tests/test_message.py tests/test_dispatcher.py tests/plugins/test_xiaoqing_chat.py tests/plugins/test_xiaoqing_chat_media.py -q
```

Expected: PASS

**Step 5: Run one broader message-flow regression**

Run:

```powershell
pytest tests/integration/test_message_flow.py -q
```

Expected: PASS

## Recommended Implementation Order

1. Task 1 first, because image-only events currently never reach the plugin.
2. Task 2 second, because outbound meme reply is pointless without a usable local emoji library and media cache.
3. Task 3 third, to make inbound image turns visible to planning and memory.
4. Task 4 fourth, to add optional outbound meme sending without destabilizing the existing text reply path.
5. Task 5 last, to lock the feature down with docs and regression coverage.

## Key Design Decision

Do **not** make the main reply model emit inline image markers and then parse them back out. That would fight against the current `xiaoqing_chat` text rewrite and postprocess assumptions. The safer design is:

- main reply model stays plain-text
- media understanding happens before prompt construction
- meme sending happens after text reply generation as a separate planner

This matches MaiBot's actual architecture more closely, but keeps the `xiaoqing_chat` implementation much smaller.
