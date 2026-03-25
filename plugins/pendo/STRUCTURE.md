# Pendo Plugin — Code Structure Reference

> Read this file at the start of a new conversation to understand the codebase without re-reading all source files.

---

## Directory Tree

```
plugins/pendo/
├── main.py              # Plugin entry point, lifecycle (init/handle/cleanup)
├── config.py            # All configuration constants
├── plugin.json          # Plugin manifest, scheduled tasks
│
├── models/
│   ├── item.py          # Dataclasses: Item, EventItem, TaskItem, NoteItem, DiaryItem, LedgerItem
│   ├── types.py         # Enums: ItemType, TaskStatus, Priority
│   └── constants.py     # Field name constants
│
├── core/
│   ├── router.py        # CommandRouter — alias resolution + dispatch
│   ├── runtime.py       # PluginContext / runtime state
│   ├── types.py         # Protocol / context type definitions
│   └── exceptions.py    # Custom exception classes
│
├── handlers/            # One handler per feature — all business logic lives here
│   ├── event.py         # EventHandler
│   ├── task.py          # TaskHandler
│   ├── note.py          # NoteHandler
│   ├── diary.py         # DiaryHandler
│   ├── ledger.py        # LedgerHandler
│   ├── search.py        # SearchHandler
│   └── web.py           # WebHandler (web UI lifecycle)
│
├── services/
│   ├── db.py            # Database — single SQLite service, connection pool, LRU cache
│   ├── reminder.py      # ReminderService — scheduling & notifications
│   ├── ai_parser.py     # AIParser — LLM-based natural language parsing
│   ├── rule_parser.py   # RuleParser — regex fallback parsing
│   ├── exporter.py      # ExporterService — Markdown import/export
│   └── llm_client.py    # LLM API client wrapper
│
├── utils/
│   ├── db_ops.py        # DbOpsMixin + get_database() singleton
│   ├── formatters.py    # ItemFormatter, paginate(), extract_kv_param()
│   ├── error_handlers.py# @handle_command_errors decorator
│   ├── session_utils.py # Multi-turn conversation session helpers
│   ├── settings_utils.py# User settings parsing
│   ├── time_utils.py    # _parse_time_range_core(), TimezoneHelper, parse_event_time_range()
│   └── validators.py    # validate_item_data(), sanitize_search_keyword()
│
├── commands/
│   ├── operations.py    # handle_confirm, handle_snooze, handle_undo
│   ├── scheduled.py     # Scheduled jobs: check_reminders, daily_briefings, etc.
│   ├── session.py       # handle_session_message — multi-turn routing
│   └── settings.py      # handle_settings
│
├── data/
│   └── pendo.db         # SQLite database file
│
└── web/                 # FastAPI web UI (runs on 127.0.0.1:8765)
    ├── server.py        # App creation, lifecycle (background thread)
    ├── auth.py          # JWT generation/verification (per-process secret key, 24h expiry)
    ├── deps.py          # FastAPI deps: get_current_user() → owner_id, get_db()
    ├── api/
    │   ├── __init__.py       # Aggregates all API routers
    │   ├── auth_routes.py    # POST /api/auth/verify
    │   ├── items.py          # GET|POST /api/items, GET|PUT|DELETE /api/items/{id}
    │   ├── dashboard.py      # GET /api/dashboard
    │   ├── search.py         # GET /api/search
    │   ├── stats.py          # GET /api/stats
    │   ├── settings.py       # GET|POST /api/settings
    │   └── config_routes.py  # GET /api/config
    └── static/              # SPA frontend
        ├── index.html
        ├── css/
        │   ├── app.css       # Global styles, module theme variables, select/filter-bar styles
        │   └── charts.css
        └── js/
            ├── app.js        # App bootstrap
            ├── api.js        # API client (adds Authorization header)
            ├── router.js     # Client-side routing
            ├── store.js      # State management
            ├── components/   # fab.js, form.js, header.js, modal.js, pagination.js, sidebar.js, toast.js
            ├── pages/        # dashboard.js, events.js, tasks.js, notes.js, diary.js, ledger.js,
            │                 #   search.js, stats.js, settings.js
            └── lib/
                └── chart-loader.js
```

---

## Data Models (models/item.py)

All items share a base `Item` dataclass. Specialised fields are in subclasses:

| Class | Key Extra Fields |
|---|---|
| `EventItem` | `start_time`, `end_time`, `location`, `rrule`, `remind_times`, `milestones` |
| `TaskItem` | `due_time`, `priority` (1–4), `status` (TODO/IN_PROGRESS/DONE/CANCELLED), `subtasks` |
| `NoteItem` | `tags`, `references`, `related_items` |
| `DiaryItem` | `mood`, `mood_score`, `weather`, `diary_date`, `template_id` |
| `LedgerItem` | `amount`, `direction` (income/expense), `ledger_category`, `ledger_date`, `remark` |

---

## Database Schema (services/db.py)

- **Engine:** SQLite with WAL mode, thread-local connections
- **Caching:** LRU cache (30 s TTL, max 1024 items)
- **Tables:**
  - `items` — all types unified, soft-delete (`is_deleted`), `owner_id` for data isolation
  - `items_fts` — FTS5 virtual table for full-text search
  - `reminder_logs` — `(item_id, remind_time, status)`
  - `operation_logs` — audit trail
  - `user_settings` — per-user timezone, quiet hours, etc.

**Key DB methods:** `get_item`, `get_items`, `insert_item`, `update_item`, `query_items_by_date_range`, `search_items`, `get_user_settings`, `update_user_settings`

---

## Command Routing Flow

```
User: /pendo event add ...
        │
        ▼
main.py handle()
  ├── Active session? → commands/session.py handle_session_message()
  └── No → core/router.py CommandRouter.route(subcommand, args, context)
               ├── Alias resolution (e.g. "e" / "日程" → "event")
               └── Dispatch → handlers/event.py EventHandler.handle()
```

Commands registered in `main.py _build_command_router()`:
`event`, `todo`, `note`, `diary`, `ledger`, `search`, `export`, `import`, `settings`, `confirm`, `snooze`, `undo`, `web`, `help`

---

## Web Auth Model

- `web/auth.py` — `generate_token(user_id)` creates a JWT signed with a **per-process random secret** (tokens expire on server restart + 24 h max)
- `web/deps.py` — `get_current_user()` FastAPI dep extracts `owner_id` from Bearer token
- **All API handlers inject `owner_id`** into every DB query → complete per-user data isolation
- Group members each get a token for their own `user_id` — no cross-user data leakage

---

## Module Theme Colors (CSS variables)

| Module | Color | CSS variable |
|---|---|---|
| Ledger | `#EF4444` | `--color-ledger` |
| Tasks | `#10B981` | `--color-tasks` |
| Notes | `#3B82F6` | `--color-notes` |
| Diary | `#EC4899` | `--color-diary` |
| Events | `#F59E0B` | `--color-events` |

---

## Key Patterns & Conventions

**Error handling:** All handler methods use `@handle_command_errors` decorator → returns `{"status": "error", "message": "..."}` on exception.

**Time parsing:** Central utility is `utils/time_utils.py _parse_time_range_core(keyword, now)` → `(start_dt, end_dt)`. Supports: `今天`, `本周`, `本月`, `今年`, `lastNd`, `yyyy-mm-dd..yyyy-mm-dd`. Handlers call this (directly or via their own `_parse_X_range` wrapper).

**Filter parsing in list commands:** All list handlers parse filter tokens from the `args` string before the DB call. Pattern: split on spaces, pull out `key:value` tokens, pass remainder as the range/keyword. Supported filters vary per module:
- `ledger list`: `dir:收入/支出`, `cat:分类`, `amount:N`, `amount:N..M`, `ex` (show max-per-cat)
- `event list`: `cat:分类`, `#tag`
- `note list`: `since:时间范围`
- `task list`: `p:优先级`
- `diary list`: `mood:情绪`

**Multi-turn sessions:** Created by handlers (diary template, ledger interactive add). Stored in `PluginContext`. Resumed via `commands/session.py`.

**AI parsing:** `services/ai_parser.py` used for event/task natural language input. Falls back to `services/rule_parser.py` on timeout or low confidence.

**Scheduled jobs (plugin.json):** Trigger `scheduled`, `scheduled_daily_briefing`, `scheduled_diary_reminder`, `scheduled_migrate_todos` — all handled in `commands/scheduled.py`.

---

## Frequently Modified Files

| Task | Files to read |
|---|---|
| Add/change a CLI command | `handlers/<module>.py`, `core/router.py`, `main.py` |
| Fix time/date parsing | `utils/time_utils.py`, handler's `_parse_X_range` method |
| Web API changes | `web/api/<route>.py`, `web/deps.py` |
| Web UI page changes | `web/static/js/pages/<page>.js`, `web/static/css/app.css` |
| DB schema / query | `services/db.py` |
| Reminder logic | `services/reminder.py`, `commands/scheduled.py` |
| Config values | `config.py` |
