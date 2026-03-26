# Task Page Redesign Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Rebuild the Pendo task page into a green execution-focused workspace with compact task insights and fully validated task CRUD.

**Architecture:** Add a task-specific overview aggregator for the web UI, then replace the current kanban-only page with a hybrid page: compact insight cards at the top, an execution-first list as the primary workspace, and a secondary kanban view for status flow. Tighten task create/update/delete rules on the backend so UI and storage use the same normalized task model.

**Tech Stack:** FastAPI, existing unified `items` API, SQLite service layer, vanilla JS SPA, project custom selects/modal components, inline CSS in page module.

---

### Task 1: Lock task CRUD failures with tests

**Files:**
- Modify: `tests/plugins/test_pendo_web_items.py`

**Step 1: Write failing tests**

Add regression tests for:
- task normalization accepts priorities `1-5`
- invalid status is rejected
- invalid due time is rejected
- moving a task to `done` sets `completed_at`
- moving a task out of `done` clears `completed_at`

**Step 2: Run tests to verify failures**

Run: `pytest tests/plugins/test_pendo_web_items.py -v`

Expected: failures around missing task normalization behavior.

**Step 3: Implement minimal normalization hooks**

Add `normalize_task_fields()` in validators and wire it into `create_item()` / `update_item()`.

**Step 4: Run tests to verify they pass**

Run: `pytest tests/plugins/test_pendo_web_items.py -v`

Expected: PASS for new task cases.

### Task 2: Add task overview aggregation for the new page

**Files:**
- Create: `plugins/pendo/web/analytics/task_overview.py`
- Modify: `plugins/pendo/web/api/stats.py`
- Test: `tests/plugins/test_pendo_web_tasks.py`

**Step 1: Write failing tests**

Cover:
- overview summary counts (`focus`, `overdue`, `done today`, `done this week`)
- due buckets (`today`, `next`, `later`, `no due`)
- category load and completion rhythm
- source assertion for `/stats/tasks/overview`

**Step 2: Run tests to verify failures**

Run: `pytest tests/plugins/test_pendo_web_tasks.py -v`

Expected: FAIL because overview builder and route do not exist.

**Step 3: Implement overview builder and route**

Return:
- `summary`
- `focus_tasks`
- `up_next_tasks`
- `overdue_tasks`
- `done_recent`
- `category_load`
- `completion_bars`
- `board_columns`

**Step 4: Run tests to verify they pass**

Run: `pytest tests/plugins/test_pendo_web_tasks.py -v`

Expected: PASS.

### Task 3: Replace the task page frontend

**Files:**
- Modify: `plugins/pendo/web/static/js/pages/tasks.js`

**Step 1: Keep current affordances but redesign layout**

Build:
- hero header
- compact insight cards
- primary task list grouped by execution buckets
- secondary kanban toggle
- polished empty states

**Step 2: Hook page to new overview route**

Use `/stats/tasks/overview` as the main fetch source, and keep updates reloading the page state after create/update/delete/drag.

**Step 3: Preserve project UI conventions**

Use:
- existing modal
- existing custom select
- warm green task theme
- compact responsive layout consistent with dashboard/events/ledger

**Step 4: Verify syntax**

Run: `node --check plugins/pendo/web/static/js/pages/tasks.js`

Expected: PASS.

### Task 4: Tighten task modal behavior

**Files:**
- Modify: `plugins/pendo/web/static/js/pages/tasks.js`
- Modify: `plugins/pendo/web/static/js/components/form.js` (only if priority widget needs 5th level support)

**Step 1: Update task editor to match normalized model**

Ensure:
- priority supports 1-5 visually and in submission
- status changes reflect `completed_at`
- invalid inputs show useful messages
- delete uses existing confirm modal

**Step 2: Re-run task CRUD tests and page syntax**

Run:
- `pytest tests/plugins/test_pendo_web_items.py tests/plugins/test_pendo_web_tasks.py -v`
- `node --check plugins/pendo/web/static/js/pages/tasks.js`

Expected: PASS.

### Task 5: Final verification

**Files:**
- Verify only

**Step 1: Run full relevant verification**

Run:
- `pytest tests/plugins/test_pendo_web_items.py tests/plugins/test_pendo_web_tasks.py tests/plugins/test_pendo_dashboard.py -v`
- `node --check plugins/pendo/web/static/js/pages/tasks.js`

Expected: PASS.

**Step 2: Manual browser follow-up**

Check:
- create task
- edit task
- drag between statuses
- delete task
- filters and view toggle
- overview cards follow data changes
