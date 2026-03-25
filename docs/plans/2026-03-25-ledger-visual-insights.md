# Ledger Visual Insights Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add compact, expense-first visual insights to the Pendo ledger page that redraw on every active filter change.

**Architecture:** Add a dedicated ledger insights aggregation helper on the backend so the ledger page can request one filtered analytics payload. Render four compact SVG cards on the frontend: expense pulse, category ring, hotspot ranking, and a mini expense candlestick chart. Keep the visual language aligned with the existing ledger page rather than reusing the generic stats page.

**Tech Stack:** FastAPI route, SQLite aggregation via existing `Database`, vanilla JS, inline SVG, existing Pendo CSS variables.

---

### Task 1: Backend ledger insights aggregation

**Files:**
- Create: `plugins/pendo/web/analytics/__init__.py`
- Create: `plugins/pendo/web/analytics/ledger_insights.py`
- Modify: `plugins/pendo/web/api/stats.py`
- Test: `tests/plugins/test_pendo_web_items.py`

**Step 1: Write the failing test**

Add a regression test that builds a temporary ledger database with multiple expense categories and dates, then calls the pure helper from `ledger_insights.py` and asserts:
- filtered category results use `ledger_category`
- returned trend buckets contain expense totals
- returned category totals are sorted descending

**Step 2: Run test to verify it fails**

Run: `pytest tests/plugins/test_pendo_web_items.py -k ledger_insights -v`
Expected: FAIL because helper module does not exist yet.

**Step 3: Write minimal implementation**

Implement:
- ledger filter SQL builder using `ledger_category`
- bucket generation (`daily` for short ranges, `monthly` for long ranges)
- expense timeline
- expense category totals + shares
- hotspot ranking
- expense candlestick data using first/last/max/min expense per bucket
- summary metrics and previous-period delta

Expose route:
- `GET /stats/ledger/insights`
- accepts current ledger page filters (`direction`, `category`, `start_date`, `end_date`, `amount_min`, `amount_max`)

**Step 4: Run test to verify it passes**

Run: `pytest tests/plugins/test_pendo_web_items.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add plugins/pendo/web/analytics/__init__.py plugins/pendo/web/analytics/ledger_insights.py plugins/pendo/web/api/stats.py tests/plugins/test_pendo_web_items.py
git commit -m "feat: add ledger insights analytics payload"
```

### Task 2: Frontend compact SVG insights area

**Files:**
- Create: `plugins/pendo/web/static/js/components/ledger_insights.js`
- Modify: `plugins/pendo/web/static/js/pages/ledger.js`

**Step 1: Write the failing test**

Use a source-level regression check in `tests/plugins/test_pendo_web_items.py` asserting the ledger page requests `/stats/ledger/insights` and renders the new insights component.

**Step 2: Run test to verify it fails**

Run: `pytest tests/plugins/test_pendo_web_items.py -k source -v`
Expected: FAIL because the route/component is not referenced yet.

**Step 3: Write minimal implementation**

Implement:
- a compact insights section between the filter bar and list
- four SVG cards:
  - expense pulse area chart
  - category ring chart
  - hotspot capsule ranking
  - mini expense K-line card
- graceful empty states for filters with no expense data
- redraw on every `loadAndRender()`

**Step 4: Run test to verify it passes**

Run: `pytest tests/plugins/test_pendo_web_items.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add plugins/pendo/web/static/js/components/ledger_insights.js plugins/pendo/web/static/js/pages/ledger.js tests/plugins/test_pendo_web_items.py
git commit -m "feat: add compact ledger visual insights"
```

### Task 3: Verification and polish

**Files:**
- Modify: `plugins/pendo/web/static/js/pages/ledger.js`
- Modify: `plugins/pendo/web/static/js/components/ledger_insights.js`

**Step 1: Verify redraw and empty-state behavior**

Manually verify:
- changing date range redraws all charts
- changing category filter redraws all charts
- no-expense states show empty messaging instead of broken SVG

**Step 2: Run automated verification**

Run: `pytest tests/plugins/test_pendo_web_items.py`
Expected: PASS

**Step 3: Final polish**

Tighten card spacing, labels, and color balance if any chart feels too heavy relative to the ledger page.

**Step 4: Commit**

```bash
git add plugins/pendo/web/static/js/pages/ledger.js plugins/pendo/web/static/js/components/ledger_insights.js tests/plugins/test_pendo_web_items.py
git commit -m "style: polish ledger insights layout"
```
