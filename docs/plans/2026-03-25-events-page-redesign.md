# Events Page Redesign Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Rebuild the Pendo events page around a responsive monthly calendar, expandable day timeline, richer filters, and full event/reminder management for single and milestone events.

**Architecture:** Add an events-specific web aggregation layer that returns month-range event data, derived calendar/day timeline metadata, and reminder log summaries. Replace the current minimal `events.js` page with a denser calendar + timeline UI that uses shared modal patterns for details and editing while still writing through the existing item CRUD endpoints where practical.

**Tech Stack:** FastAPI routers, SQLite-backed `Database`, existing Pendo item model, vanilla JS SPA page modules, shared modal/toast/router utilities.

---

### Task 1: Add event-focused web aggregation and API routes

**Files:**
- Create: `plugins/pendo/web/analytics/events_overview.py`
- Create: `plugins/pendo/web/api/events.py`
- Modify: `plugins/pendo/web/api/__init__.py`
- Test: `tests/plugins/test_pendo_web_events.py`

**Step 1: Write failing tests**

- Cover month-range overview aggregation for:
  - single events
  - milestone events
  - recurring instances
  - reminder log status mapping
  - category/kind/reminder filters
- Cover detail payload with reminder log rows.

**Step 2: Implement aggregation helpers**

- Fetch events in range with `db.get_events_for_range(...)` plus recurring instances.
- Normalize them into web payloads with:
  - kind (`single`, `milestone`, `recurring_instance`)
  - day buckets
  - reminder summary/status counts
  - milestone timeline rows

**Step 3: Implement API routes**

- `GET /events/overview`
- `GET /events/categories`
- `GET /events/{id}/detail`

**Step 4: Run tests**

- `pytest tests/plugins/test_pendo_web_events.py -v`

### Task 2: Extend generic item API validation for milestone event editing

**Files:**
- Modify: `plugins/pendo/web/api/items.py`
- Test: `tests/plugins/test_pendo_web_items.py`

**Step 1: Write/extend tests**

- Verify event create/update accepts `milestones` and `remind_times`.
- Verify milestone events are persisted through the web API.

**Step 2: Implement minimal schema updates**

- Add `milestones` to `ItemCreate` and `ItemUpdate`.
- Preserve existing behavior for other item types.

**Step 3: Run tests**

- `pytest tests/plugins/test_pendo_web_items.py -v`

### Task 3: Rebuild the events page UI

**Files:**
- Modify: `plugins/pendo/web/static/js/pages/events.js`

**Step 1: Replace page state model**

- Month/year navigation
- Selected day
- Filters: range, keyword, category, kind, reminder state
- View mode: calendar / timeline list

**Step 2: Build the new calendar shell**

- Responsive month calendar
- Cell-level event chips on wide layouts
- Dot/count fallback on tight layouts
- Selected day expansion

**Step 3: Build reusable day timeline rendering**

- Shared timeline renderer for calendar day detail and list mode
- Support single events, milestone nodes, recurring instances
- Click item to open detail modal

**Step 4: Build detail and edit flows**

- Detail modal with event metadata, milestones, reminders, reminder statuses
- Edit/create modal with single vs milestone modes
- Reminder CRUD via event update payloads
- Event delete with existing themed confirm modal

**Step 5: Run syntax checks**

- `node --check plugins/pendo/web/static/js/pages/events.js`

### Task 4: Final verification

**Files:**
- Review: `plugins/pendo/web/static/js/pages/events.js`
- Review: `plugins/pendo/web/api/events.py`
- Review: `plugins/pendo/web/analytics/events_overview.py`

**Step 1: Run focused test set**

- `pytest tests/plugins/test_pendo_web_events.py tests/plugins/test_pendo_web_items.py -v`

**Step 2: Check JS syntax**

- `node --check plugins/pendo/web/static/js/pages/events.js`

**Step 3: Manual QA notes**

- Verify month calendar navigation
- Verify day timeline expansion
- Verify detail modal, reminder CRUD, and event CRUD
- Verify filters redraw both calendar and list timeline
