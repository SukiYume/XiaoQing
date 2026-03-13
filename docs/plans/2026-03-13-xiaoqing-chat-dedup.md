# XiaoQing Chat Dedup Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Reduce confirmed duplication in `plugins/xiaoqing_chat` without changing user-visible behavior.

**Architecture:** Extract shared helpers for repeated JSON-response parsing and repeated fallback/flush orchestration, then switch current call sites to those helpers. Keep the refactor local to `xiaoqing_chat`, avoid cross-plugin abstractions, and defer larger architectural cleanup that would change persistence/state boundaries.

**Tech Stack:** Python 3.10+, pytest, asyncio, pydantic

---

### Task 1: Centralize embedded JSON object parsing

**Files:**
- Create: `plugins/xiaoqing_chat/utils/json_parsing.py`
- Modify: `plugins/xiaoqing_chat/memory/memory_retrieval.py`
- Modify: `plugins/xiaoqing_chat/memory/knowledge_extract.py`
- Modify: `plugins/xiaoqing_chat/llm/reply_checker.py`
- Test: `tests/plugins/test_xiaoqing_chat_dedup.py`

**Step 1: Write the failing test**

Add tests for:
- extracting the first JSON object from text with leading/trailing prose
- parsing `question` from embedded JSON
- parsing `items` / `facts` arrays from embedded JSON
- parsing reply-check JSON through the new helper path

**Step 2: Run test to verify it fails**

Run: `pytest tests/plugins/test_xiaoqing_chat_dedup.py -k json`
Expected: FAIL because helper/module does not exist yet.

**Step 3: Write minimal implementation**

Create a helper that:
- extracts the first embedded JSON object string
- parses it to `dict[str, Any] | None`
- optionally returns a named list field when present

Switch `_parse_question_json`, `_parse_word_json`, `_parse_fact_json`, and the JSON loading in `reply_checker.py` to use the helper.

**Step 4: Run test to verify it passes**

Run: `pytest tests/plugins/test_xiaoqing_chat_dedup.py -k json`
Expected: PASS.

### Task 2: Remove duplicate LLM response extraction / fallback wrapper logic

**Files:**
- Modify: `plugins/xiaoqing_chat/llm/llm_client.py`
- Modify: `plugins/xiaoqing_chat/llm/reply_checker.py`
- Test: `tests/plugins/test_xiaoqing_chat_dedup.py`

**Step 1: Write the failing test**

Add tests for:
- shared response-content extraction from `message` and `delta`
- shared fallback-path behavior for content and raw modes
- `reply_checker` using the shared response-content extraction path

**Step 2: Run test to verify it fails**

Run: `pytest tests/plugins/test_xiaoqing_chat_dedup.py -k llm_client`
Expected: FAIL because the helper APIs are not unified yet.

**Step 3: Write minimal implementation**

In `llm_client.py`:
- extract one shared helper for selecting response message payload
- expose one shared content extractor for other modules
- extract one internal helper for trying fallback endpoint paths

Update `reply_checker.py` to use the shared content extractor instead of hardcoded `choices[0].message.content` traversal.

**Step 4: Run test to verify it passes**

Run: `pytest tests/plugins/test_xiaoqing_chat_dedup.py -k llm_client`
Expected: PASS.

### Task 3: Merge duplicate debounced flush scheduling logic

**Files:**
- Modify: `plugins/xiaoqing_chat/task_scheduler.py`
- Test: `tests/plugins/test_xiaoqing_chat_dedup.py`
- Existing coverage touchpoint: `tests/plugins/test_xiaoqing_chat.py`

**Step 1: Write the failing test**

Add tests proving both `_schedule_action_history_flush` and `_schedule_pfc_state_flush` still:
- debounce per chat id
- dispatch their flush via `asyncio.to_thread`
- clean up task registries after completion

**Step 2: Run test to verify it fails**

Run: `pytest tests/plugins/test_xiaoqing_chat_dedup.py -k flush`
Expected: FAIL until the shared helper is introduced.

**Step 3: Write minimal implementation**

Extract a shared internal helper like `_schedule_chat_flush_task(...)` that accepts:
- task registry
- task name prefix
- flush callable

Keep `_schedule_action_history_flush` and `_schedule_pfc_state_flush` as thin wrappers so existing call sites and tests stay stable.

**Step 4: Run test to verify it passes**

Run: `pytest tests/plugins/test_xiaoqing_chat_dedup.py -k flush`
Expected: PASS.

### Task 4: Optional low-risk save-path dedup if still small after earlier tasks

**Files:**
- Modify: `plugins/xiaoqing_chat/memory/vector_store.py`
- Modify: `plugins/xiaoqing_chat/memory/memory_db.py`
- Test: `tests/plugins/test_xiaoqing_chat_dedup.py`

**Step 1: Write the failing test**

Add a test proving `MemoryDB.save()` and `VectorStore.save()` still write equivalent docs/vector payload structure.

**Step 2: Run test to verify it fails**

Run: `pytest tests/plugins/test_xiaoqing_chat_dedup.py -k save`
Expected: FAIL if the shared write helper is not yet present.

**Step 3: Write minimal implementation**

Only if the refactor remains small and obvious, extract a shared write helper in `vector_store.py` for docs + compressed matrix output and reuse it from `memory_db.py`.

If the change starts expanding lock/snapshot semantics, defer this task.

**Step 4: Run test to verify it passes**

Run: `pytest tests/plugins/test_xiaoqing_chat_dedup.py -k save`
Expected: PASS, or explicitly defer task if semantics become broader than dedup.

### Task 5: Full verification

**Files:**
- Verify: `plugins/xiaoqing_chat/utils/json_parsing.py`
- Verify: `plugins/xiaoqing_chat/memory/memory_retrieval.py`
- Verify: `plugins/xiaoqing_chat/memory/knowledge_extract.py`
- Verify: `plugins/xiaoqing_chat/llm/llm_client.py`
- Verify: `plugins/xiaoqing_chat/llm/reply_checker.py`
- Verify: `plugins/xiaoqing_chat/task_scheduler.py`
- Verify: `plugins/xiaoqing_chat/memory/memory_db.py`
- Verify: `plugins/xiaoqing_chat/memory/vector_store.py`
- Verify: `tests/plugins/test_xiaoqing_chat_dedup.py`

**Step 1: Run targeted tests**

Run: `pytest tests/plugins/test_xiaoqing_chat_dedup.py`

**Step 2: Run relevant existing regression tests**

Run: `pytest tests/plugins/test_xiaoqing_chat_review_regressions.py`

**Step 3: Check diagnostics**

Run LSP diagnostics on every changed file.

**Step 4: Report deferrals explicitly**

If any section-2 items remain intentionally unfixed (cross-plugin LLM client sharing, state-file consolidation, goal-management unification), call them out as deferred and explain why.
