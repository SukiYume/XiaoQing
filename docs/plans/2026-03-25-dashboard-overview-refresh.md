# Dashboard Overview Refresh Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Refresh the Pendo dashboard so it feels denser and more useful, with month-scoped events, a task panel that includes a few completed tasks, and a more intentional visual layout.

**Architecture:** Update the dashboard API to return month event counts/listing plus split task data into active and recently completed buckets. Redesign the dashboard page to consume the richer payload and render a denser two-column overview with styled summary cards, richer section states, and an upgraded spending panel.

**Tech Stack:** FastAPI, existing `Database` service queries, vanilla JS page modules, inline component styles in `dashboard.js`, pytest.

---

### Task 1: Extend Dashboard API Payload

**Files:**
- Modify: `plugins/pendo/web/api/dashboard.py`
- Test: `tests/plugins/test_pendo_dashboard.py`

**Step 1: Write the failing test**

Add a focused dashboard aggregation test that seeds:
- multiple events in the current month and outside it
- pending/in-progress tasks
- completed tasks
- ledger records
- diary entries

Assert that the API helper/build logic returns:
- `summary.events_month`
- `summary.tasks_done_recent`
- `events_month`
- `tasks.active`
- `tasks.completed`

**Step 2: Run test to verify it fails**

Run: `pytest tests/plugins/test_pendo_dashboard.py -v`
Expected: FAIL because the new keys do not exist yet.

**Step 3: Write minimal implementation**

In `dashboard.py`:
- replace “today’s events” query with current-month events query
- keep event ordering stable by `start_time`
- fetch active tasks (`todo`, `in_progress`)
- fetch a few recently completed tasks ordered by `completed_at` or `updated_at`
- update summary counts accordingly
- keep existing ledger and diary summary behavior unless a direct bug is found

**Step 4: Run test to verify it passes**

Run: `pytest tests/plugins/test_pendo_dashboard.py -v`
Expected: PASS

### Task 2: Redesign Dashboard Page Structure

**Files:**
- Modify: `plugins/pendo/web/static/js/pages/dashboard.js`

**Step 1: Write the failing source assertions**

Add test assertions in `tests/plugins/test_pendo_dashboard.py` that the dashboard page references:
- month events summary labels
- completed task rendering
- the new payload keys

**Step 2: Run test to verify it fails**

Run: `pytest tests/plugins/test_pendo_dashboard.py -v`
Expected: FAIL because the old dashboard page still uses today-only labels and a single task list.

**Step 3: Write minimal implementation**

In `dashboard.js`:
- change summary card copy from `今日日程` to `本月日程`
- redesign page spacing/card layout to reduce empty feeling
- replace the single event timeline with a “monthly agenda” card that can show more rows and better empty copy
- split tasks into “进行中/待办” and “最近完成” sections
- preserve task complete interaction for active tasks only
- make the ledger chart and finance panel visually consistent with the denser layout

**Step 4: Run test to verify it passes**

Run: `pytest tests/plugins/test_pendo_dashboard.py -v`
Expected: PASS

### Task 3: Verification

**Files:**
- Test: `tests/plugins/test_pendo_dashboard.py`

**Step 1: Run targeted tests**

Run: `pytest tests/plugins/test_pendo_dashboard.py tests/plugins/test_pendo_web_items.py -v`
Expected: PASS

**Step 2: Run JS syntax checks**

Run:
- `node --check plugins/pendo/web/static/js/pages/dashboard.js`
- `node --check plugins/pendo/web/static/js/pages/ledger.js`

Expected: PASS

**Step 3: Commit**

```bash
git add docs/plans/2026-03-25-dashboard-overview-refresh.md plugins/pendo/web/api/dashboard.py plugins/pendo/web/static/js/pages/dashboard.js tests/plugins/test_pendo_dashboard.py
git commit -m "feat: refresh dashboard overview layout"
```
