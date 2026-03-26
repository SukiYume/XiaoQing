# Pendo Web Follow-up Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Continue the Pendo Web overhaul after dashboard, events, tasks, and core ledger logic, and bring the remaining pages and shared chrome to the same product quality level.

**Architecture:** Keep the current SPA + FastAPI structure, but move each page toward the same pattern: themed hero/header, compact insight area, stronger list/detail layout, and page-specific analytics built from lightweight backend aggregations when necessary. Reuse shared modal/select/form primitives where possible and collapse repeated page-level styling/logic only after the page behaviors are correct.

**Tech Stack:** FastAPI, SQLite, vanilla JS SPA, shared custom select/modal/form components, SVG-first visualizations, targeted pytest regression tests, Python `py_compile`, `node --check`.

---

## Execution Order

- [ ] `ledger` visual alignment and cleanup
- [ ] `notes` redesign + CRUD audit/fixes
- [ ] `diary` redesign + CRUD audit/fixes
- [ ] `search` redesign + smarter interaction/results
- [ ] `stats` redesign + richer visual analytics
- [ ] `settings` redesign + config CRUD audit/fixes
- [ ] `login/auth` redesign + token contract audit/fixes
- [ ] `sidebar` visual refinement after each page round
- [ ] global logic / redundancy / regression sweep

## Task 1: Ledger Visual Alignment

**Files:**
- Modify: `plugins/pendo/web/static/js/pages/ledger.js`
- Modify: `plugins/pendo/web/static/js/components/ledger_insights.js`
- Modify: `plugins/pendo/web/static/css/app.css`

**Intent:**
- Keep current ledger filtering/query logic.
- Align card spacing, typography hierarchy, control rhythm, and section framing with `dashboard/events/tasks`.
- Preserve ledger theme color and existing SVG insight cards.

**Checks:**
- `node --check plugins/pendo/web/static/js/pages/ledger.js`
- `node --check plugins/pendo/web/static/js/components/ledger_insights.js`

## Task 2: Notes Page Redesign + CRUD Audit

**Files:**
- Modify: `plugins/pendo/web/static/js/pages/notes.js`
- Modify: `plugins/pendo/web/api/items.py`
- Modify: `plugins/pendo/utils/validators.py`
- Modify: `plugins/pendo/web/static/css/app.css`
- Test: `tests/plugins/test_pendo_web_items.py`

**Intent:**
- Redesign notes into a page that balances browsing and capture.
- Add compact note insights, such as category distribution, recent writing cadence, tag heat, or note-length distribution.
- Recheck note create/update/delete/query behavior, especially clearing fields, tags parsing, category filtering, and pagination consistency.

**Checks:**
- `pytest tests/plugins/test_pendo_web_items.py -q`
- `node --check plugins/pendo/web/static/js/pages/notes.js`

## Task 3: Diary Page Redesign + CRUD Audit

**Files:**
- Modify: `plugins/pendo/web/static/js/pages/diary.js`
- Modify: `plugins/pendo/web/api/items.py`
- Modify: `plugins/pendo/utils/validators.py`
- Modify: `plugins/pendo/web/static/css/app.css`
- Test: `tests/plugins/test_pendo_web_items.py`

**Intent:**
- Redesign diary into a more atmospheric page with stronger month/day navigation and richer entry previews.
- Add compact diary visuals, such as mood frequency, writing streaks, or monthly entry density.
- Recheck diary create/update/delete/query behavior, including date uniqueness assumptions, template usage, and clearing optional fields.

**Checks:**
- `pytest tests/plugins/test_pendo_web_items.py -q`
- `node --check plugins/pendo/web/static/js/pages/diary.js`

## Task 4: Search Page Redesign

**Files:**
- Modify: `plugins/pendo/web/static/js/pages/search.js`
- Modify: `plugins/pendo/web/api/search.py`
- Modify: `plugins/pendo/web/static/css/app.css`

**Intent:**
- Make search feel more like a command center instead of a plain result list.
- Improve empty states, ranking cues, filters, result snippets, and keyboard-friendly interaction.
- If needed, add grouped result sections, smarter query summaries, and quick route-jump actions.

**Checks:**
- `node --check plugins/pendo/web/static/js/pages/search.js`

## Task 5: Stats Page Redesign

**Files:**
- Modify: `plugins/pendo/web/static/js/pages/stats.js`
- Modify: `plugins/pendo/web/api/stats.py`
- Modify/Create: `plugins/pendo/web/analytics/*.py`
- Modify: `plugins/pendo/web/static/css/app.css`
- Test: `tests/plugins/test_pendo_dashboard.py`
- Test: `tests/plugins/test_pendo_web_tasks.py`
- Test: `tests/plugins/test_pendo_web_events.py`
- Test: `tests/plugins/test_pendo_web_items.py`

**Intent:**
- Replace the current generic chart page with a fuller analytics workspace.
- Favor themed SVG or tightly-controlled chart usage over default-looking generic dashboards.
- Add denser summaries for ledger/tasks/events/notes/diary where useful, without losing readability.
- Second pass should expand chart variety beyond simple line/bar cards:
  - `Treemap` for category composition
  - `Histogram` for ledger amount bands
  - `Stacked columns` for task intake vs completion
  - `Weekday × time-slot heatmap` for events
  - Keep the masonry wall and mix chart heights/types intentionally
- Third pass should harden chart rendering and range behavior:
  - Sample/compress dense x-axis labels instead of rendering every tick
  - Use fixed-height column stages so vertical charts do not collapse into short pills
  - Replace the fragile donut stroke math with deterministic arc-path rendering
  - Audit stats calculations for weekly task created/completed buckets and category ordering
  - Add `去年` and `自定义` range controls and keep the summary/header dates aligned with the selected range

**Checks:**
- `pytest tests/plugins/test_pendo_web_items.py tests/plugins/test_pendo_web_events.py tests/plugins/test_pendo_web_tasks.py tests/plugins/test_pendo_dashboard.py -q`
- `pytest tests/plugins/test_pendo_web_stats.py -q`
- `node --check plugins/pendo/web/static/js/pages/stats.js`

## Task 6: Settings Page Redesign + CRUD Audit

**Files:**
- Modify: `plugins/pendo/web/static/js/pages/settings.js`
- Modify: `plugins/pendo/web/api/settings.py`
- Modify: `plugins/pendo/web/static/css/app.css`
- Test: `tests/plugins/test_pendo_dashboard.py`

**Intent:**
- Redesign settings into a calmer control panel with better grouping and explanation hierarchy.
- Recheck settings load/save behavior, toggle persistence, time-range persistence, and logout/clear-token behavior.

**Checks:**
- `node --check plugins/pendo/web/static/js/pages/settings.js`

## Task 7: Sidebar Refresh

**Files:**
- Modify: `plugins/pendo/web/static/js/components/sidebar.js`
- Modify: `plugins/pendo/web/static/css/app.css`

**Intent:**
- After each page round, keep sidebar visually in sync with the updated page language.
- Improve active state, spacing, icon treatment, section separation, and responsive behavior without changing route structure.

**Checks:**
- `node --check plugins/pendo/web/static/js/components/sidebar.js`

## Task 8: Login/Auth Refresh + Token Contract Audit

**Files:**
- Modify: `plugins/pendo/web/static/index.html`
- Modify: `plugins/pendo/web/static/css/app.css`
- Modify: `plugins/pendo/web/static/js/app.js`
- Modify: `plugins/pendo/web/static/js/api.js`
- Modify: `plugins/pendo/web/auth.py`
- Modify: `plugins/pendo/web/deps.py`
- Modify: `plugins/pendo/handlers/web.py`
- Modify: `plugins/pendo/config.py`
- Test: `tests/plugins/test_pendo_web_auth.py`

**Intent:**
- Replace the current plain token input with a login screen that matches the updated product language.
- Make token lifetime behavior stable across server restarts by removing the per-process secret contract.
- Recheck token generation, verification, authorization header parsing, and frontend token persistence/expiry handling.

**Checks:**
- `pytest tests/plugins/test_pendo_web_auth.py -q`
- `node --check plugins/pendo/web/static/js/app.js`
- `node --check plugins/pendo/web/static/js/api.js`
- `python -m py_compile plugins/pendo/web/auth.py plugins/pendo/web/deps.py plugins/pendo/handlers/web.py`

## Task 9: Global Logic / Redundancy Sweep

**Files:**
- Inspect all touched `plugins/pendo/web/static/js/pages/*.js`
- Inspect shared components in `plugins/pendo/web/static/js/components/`
- Inspect `plugins/pendo/web/api/*.py`
- Inspect `plugins/pendo/web/analytics/*.py`

**Intent:**
- Remove repeated helpers where practical.
- Recheck CRUD edge cases across note/diary/settings/search/stats integrations.
- Verify shared components are reused consistently.

**Checks:**
- `git diff --check`
- `pytest tests/plugins/test_pendo_web_items.py tests/plugins/test_pendo_web_events.py tests/plugins/test_pendo_web_tasks.py tests/plugins/test_pendo_dashboard.py -q`
- `python -m py_compile plugins/pendo/models/item.py plugins/pendo/services/db.py plugins/pendo/utils/validators.py plugins/pendo/web/api/__init__.py plugins/pendo/web/api/dashboard.py plugins/pendo/web/api/events.py plugins/pendo/web/api/items.py plugins/pendo/web/api/search.py plugins/pendo/web/api/settings.py plugins/pendo/web/api/stats.py plugins/pendo/web/analytics/dashboard_overview.py plugins/pendo/web/analytics/events_overview.py plugins/pendo/web/analytics/ledger_insights.py plugins/pendo/web/analytics/task_overview.py`
