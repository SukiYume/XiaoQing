# Pendo Web UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a full-featured Web UI for the pendo plugin with dashboard, CRUD management for all 5 data types, search, statistics/charts, and settings.

**Architecture:** FastAPI backend serving a REST API + static SPA frontend. The API layer reuses the existing `Database` class from `services/db.py` directly. Frontend is vanilla HTML/CSS/JS with ES Modules and Chart.js for visualization. JWT token auth generated via chat command.

**Tech Stack:** Python (FastAPI, uvicorn, PyJWT), vanilla JS (ES Modules), Chart.js 4.x, HTML5, CSS3

**Spec:** `docs/superpowers/specs/2026-03-24-pendo-webui-design.md`

---

## File Structure

### Backend (New Files)

| File | Responsibility |
|------|---------------|
| `plugins/pendo/web/__init__.py` | Package init |
| `plugins/pendo/web/server.py` | FastAPI app, uvicorn lifecycle (start/stop in thread) |
| `plugins/pendo/web/auth.py` | JWT token generate/verify, SECRET_KEY management |
| `plugins/pendo/web/deps.py` | FastAPI dependencies: get_db(), get_current_user() |
| `plugins/pendo/web/api/__init__.py` | APIRouter aggregation |
| `plugins/pendo/web/api/auth_routes.py` | POST /api/auth/verify |
| `plugins/pendo/web/api/items.py` | Unified CRUD: GET/POST/PUT/DELETE /api/items |
| `plugins/pendo/web/api/dashboard.py` | GET /api/dashboard |
| `plugins/pendo/web/api/search.py` | GET /api/search |
| `plugins/pendo/web/api/stats.py` | GET /api/stats/{type} |
| `plugins/pendo/web/api/settings.py` | GET/PUT /api/settings |
| `plugins/pendo/web/api/config_routes.py` | GET /api/config/categories, /api/diary/templates |
| `plugins/pendo/handlers/web.py` | Chat command handler for /pendo web start|stop|token|status |

### Frontend (New Files)

| File | Responsibility |
|------|---------------|
| `plugins/pendo/web/static/index.html` | SPA shell, layout skeleton |
| `plugins/pendo/web/static/css/app.css` | Global styles, CSS variables, layout, components |
| `plugins/pendo/web/static/css/charts.css` | Chart container styles |
| `plugins/pendo/web/static/js/app.js` | SPA bootstrap, init |
| `plugins/pendo/web/static/js/api.js` | Fetch wrapper with token auth |
| `plugins/pendo/web/static/js/router.js` | Hash-based SPA router |
| `plugins/pendo/web/static/js/store.js` | Simple global state |
| `plugins/pendo/web/static/js/pages/dashboard.js` | Dashboard overview |
| `plugins/pendo/web/static/js/pages/events.js` | Events calendar + CRUD |
| `plugins/pendo/web/static/js/pages/tasks.js` | Tasks kanban + CRUD |
| `plugins/pendo/web/static/js/pages/ledger.js` | Ledger list + quick add |
| `plugins/pendo/web/static/js/pages/notes.js` | Notes card grid |
| `plugins/pendo/web/static/js/pages/diary.js` | Diary timeline |
| `plugins/pendo/web/static/js/pages/search.js` | Cross-type search |
| `plugins/pendo/web/static/js/pages/stats.js` | Charts (ledger/tasks/events) |
| `plugins/pendo/web/static/js/pages/settings.js` | User settings |
| `plugins/pendo/web/static/js/components/sidebar.js` | Navigation sidebar |
| `plugins/pendo/web/static/js/components/header.js` | Top header bar |
| `plugins/pendo/web/static/js/components/modal.js` | Modal dialog |
| `plugins/pendo/web/static/js/components/toast.js` | Toast notifications |
| `plugins/pendo/web/static/js/components/form.js` | Form builder helpers |
| `plugins/pendo/web/static/js/components/pagination.js` | Pagination controls |
| `plugins/pendo/web/static/js/components/fab.js` | Floating action button |
| `plugins/pendo/web/static/js/lib/chart.min.js` | Chart.js 4.x (vendored) |

### Modified Files

| File | Changes |
|------|---------|
| `plugins/pendo/config.py` | Add WEB_ENABLED, WEB_HOST, WEB_PORT, WEB_TOKEN_EXPIRE_HOURS |
| `plugins/pendo/main.py` | Import and call web server start/stop in init/cleanup |
| `plugins/pendo/core/router.py` | Add "web" to COMMAND_META |
| `plugins/pendo/plugin.json` | No changes needed (web handler is called via router) |

### Test Files

| File | What it tests |
|------|--------------|
| `tests/plugins/test_pendo_web_auth.py` | Token generation/verification |
| `tests/plugins/test_pendo_web_api.py` | All API endpoints |

---

## Phase 1: Backend Foundation

### Task 1: Dependencies & Configuration

**Files:**
- Modify: `plugins/pendo/config.py`
- Create: `plugins/pendo/web/__init__.py`

- [ ] **Step 1: Add web config to config.py**

Add these constants to the `PendoConfig` class in `plugins/pendo/config.py`, after the existing `SESSION_EXIT_COMMANDS`:

```python
    # Web UI
    WEB_ENABLED = True
    WEB_HOST = "127.0.0.1"
    WEB_PORT = 8765
    WEB_TOKEN_EXPIRE_HOURS = 24
```

- [ ] **Step 2: Create web package**

Create `plugins/pendo/web/__init__.py`:

```python
"""Pendo Web UI - FastAPI backend + static SPA frontend."""
```

- [ ] **Step 3: Install dependencies**

```bash
pip install fastapi uvicorn[standard] PyJWT
```

- [ ] **Step 4: Commit**

```bash
git add plugins/pendo/config.py plugins/pendo/web/__init__.py
git commit -m "feat(pendo-web): add web config and web package"
```

---

### Task 2: JWT Auth Module

**Files:**
- Create: `plugins/pendo/web/auth.py`
- Create: `tests/plugins/test_pendo_web_auth.py`

- [ ] **Step 1: Write auth tests**

Create `tests/plugins/test_pendo_web_auth.py`:

```python
"""Tests for pendo web auth module."""
import time
import pytest
from plugins.pendo.web.auth import generate_token, verify_token, AuthError


class TestTokenGeneration:
    def test_generate_token_returns_string(self):
        token = generate_token("user123")
        assert isinstance(token, str)
        assert len(token) > 0

    def test_generate_token_contains_owner_id(self):
        token = generate_token("user123")
        payload = verify_token(token)
        assert payload["owner_id"] == "user123"

    def test_verify_valid_token(self):
        token = generate_token("user456")
        payload = verify_token(token)
        assert payload["owner_id"] == "user456"
        assert "exp" in payload

    def test_verify_expired_token_raises(self):
        token = generate_token("user123", expires_hours=0)
        # Token with 0 hours = already expired
        time.sleep(0.1)
        with pytest.raises(AuthError, match="expired"):
            verify_token(token)

    def test_verify_invalid_token_raises(self):
        with pytest.raises(AuthError):
            verify_token("invalid.token.string")

    def test_verify_tampered_token_raises(self):
        token = generate_token("user123")
        tampered = token[:-5] + "XXXXX"
        with pytest.raises(AuthError):
            verify_token(tampered)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd plugins/pendo && python -m pytest ../../tests/plugins/test_pendo_web_auth.py -v
```

Expected: FAIL (module not found)

- [ ] **Step 3: Implement auth module**

Create `plugins/pendo/web/auth.py`:

```python
"""JWT token generation and verification for Pendo Web UI."""
import time
import os
import jwt

# Secret key: generated per process, old tokens invalidate on restart
_SECRET_KEY = os.urandom(32).hex()
_ALGORITHM = "HS256"


class AuthError(Exception):
    """Authentication error."""
    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


def generate_token(owner_id: str, expires_hours: int = 24) -> str:
    """Generate a JWT token for the given owner_id."""
    payload = {
        "owner_id": owner_id,
        "exp": int(time.time()) + expires_hours * 3600,
        "iat": int(time.time()),
    }
    return jwt.encode(payload, _SECRET_KEY, algorithm=_ALGORITHM)


def verify_token(token: str) -> dict:
    """Verify and decode a JWT token. Returns payload dict.

    Raises AuthError if token is invalid or expired.
    """
    try:
        payload = jwt.decode(token, _SECRET_KEY, algorithms=[_ALGORITHM])
        return payload
    except jwt.ExpiredSignatureError:
        raise AuthError("Token has expired")
    except jwt.InvalidTokenError as e:
        raise AuthError(f"Invalid token: {e}")
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd plugins/pendo && python -m pytest ../../tests/plugins/test_pendo_web_auth.py -v
```

Expected: All 6 tests PASS

- [ ] **Step 5: Commit**

```bash
git add plugins/pendo/web/auth.py tests/plugins/test_pendo_web_auth.py
git commit -m "feat(pendo-web): add JWT auth module with tests"
```

---

### Task 3: FastAPI Dependencies

**Files:**
- Create: `plugins/pendo/web/deps.py`

- [ ] **Step 1: Implement deps module**

Create `plugins/pendo/web/deps.py`:

```python
"""FastAPI dependency injection for Pendo Web UI."""
from fastapi import Header, HTTPException

from ..services.db import Database
from ..utils.db_ops import get_database
from .auth import verify_token, AuthError

# Module-level reference, set by server.py on startup
_db_instance: Database | None = None


def set_db(db: Database) -> None:
    """Set the database instance (called on server start)."""
    global _db_instance
    _db_instance = db


def get_db() -> Database:
    """Get the shared Database instance."""
    if _db_instance is None:
        raise HTTPException(status_code=503, detail="Database not initialized")
    return _db_instance


def get_current_user(authorization: str = Header(...)) -> str:
    """Extract owner_id from Bearer token.

    Returns owner_id string.
    Raises 401 if token is missing, invalid, or expired.
    """
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid authorization header")
    token = authorization[7:]
    try:
        payload = verify_token(token)
        return payload["owner_id"]
    except AuthError as e:
        raise HTTPException(status_code=401, detail=e.message)
```

- [ ] **Step 2: Commit**

```bash
git add plugins/pendo/web/deps.py
git commit -m "feat(pendo-web): add FastAPI dependency injection"
```

---

### Task 4: FastAPI Server & Lifecycle

**Files:**
- Create: `plugins/pendo/web/server.py`

- [ ] **Step 1: Implement server module**

Create `plugins/pendo/web/server.py`:

```python
"""FastAPI web server for Pendo Web UI."""
import logging
import threading
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles

from ..config import PendoConfig
from ..services.db import Database
from .deps import set_db

logger = logging.getLogger("pendo.web")

_app: FastAPI | None = None
_server: uvicorn.Server | None = None
_thread: threading.Thread | None = None

STATIC_DIR = Path(__file__).parent / "static"


def create_app(db: Database) -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(title="Pendo Web UI", docs_url=None, redoc_url=None)

    # Set database instance for dependency injection
    set_db(db)

    # Custom error handler to match spec response format
    @app.exception_handler(HTTPException)
    async def http_exception_handler(request, exc):
        from fastapi.responses import JSONResponse
        return JSONResponse(
            status_code=exc.status_code,
            content={"ok": False, "message": exc.detail, "error_code": str(exc.status_code)},
        )

    # Import and mount API routes
    from .api import create_api_router
    app.include_router(create_api_router(), prefix="/api")

    # Mount static files (SPA fallback to index.html)
    if STATIC_DIR.exists():
        app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")

    return app


def start(db: Database) -> bool:
    """Start the web server in a background thread.

    Returns True if started, False if already running.
    """
    global _app, _server, _thread

    if _thread is not None and _thread.is_alive():
        return False

    _app = create_app(db)

    config = uvicorn.Config(
        _app,
        host=PendoConfig.WEB_HOST,
        port=PendoConfig.WEB_PORT,
        log_level="warning",
    )
    _server = uvicorn.Server(config)
    _thread = threading.Thread(target=_server.run, daemon=True, name="pendo-web")
    _thread.start()
    logger.info("Pendo Web UI started on http://%s:%d", PendoConfig.WEB_HOST, PendoConfig.WEB_PORT)
    return True


def stop() -> bool:
    """Stop the web server.

    Returns True if stopped, False if not running.
    """
    global _server, _thread

    if _server is None or _thread is None or not _thread.is_alive():
        return False

    _server.should_exit = True
    _thread.join(timeout=5)
    _server = None
    _thread = None
    logger.info("Pendo Web UI stopped")
    return True


def is_running() -> bool:
    """Check if the web server is running."""
    return _thread is not None and _thread.is_alive()


def get_url() -> str:
    """Get the web UI URL."""
    return f"http://{PendoConfig.WEB_HOST}:{PendoConfig.WEB_PORT}"
```

- [ ] **Step 2: Create API router aggregation**

Create `plugins/pendo/web/api/__init__.py`:

```python
"""API route aggregation for Pendo Web UI."""
from fastapi import APIRouter


def create_api_router() -> APIRouter:
    """Create the main API router with all sub-routers."""
    router = APIRouter()

    from .auth_routes import router as auth_router
    from .items import router as items_router
    from .dashboard import router as dashboard_router
    from .search import router as search_router
    from .stats import router as stats_router
    from .settings import router as settings_router
    from .config_routes import router as config_router

    router.include_router(auth_router, tags=["auth"])
    router.include_router(items_router, tags=["items"])
    router.include_router(dashboard_router, tags=["dashboard"])
    router.include_router(search_router, tags=["search"])
    router.include_router(stats_router, tags=["stats"])
    router.include_router(settings_router, tags=["settings"])
    router.include_router(config_router, tags=["config"])

    return router
```

- [ ] **Step 3: Commit**

```bash
git add plugins/pendo/web/server.py plugins/pendo/web/api/__init__.py
git commit -m "feat(pendo-web): add FastAPI server and API router"
```

---

### Task 5: API Endpoints - Auth, Items CRUD, Dashboard

**Files:**
- Create: `plugins/pendo/web/api/auth_routes.py`
- Create: `plugins/pendo/web/api/items.py`
- Create: `plugins/pendo/web/api/dashboard.py`

- [ ] **Step 1: Implement auth route**

Create `plugins/pendo/web/api/auth_routes.py`:

```python
"""Auth verification endpoint."""
from fastapi import APIRouter, Depends

from ..deps import get_current_user

router = APIRouter()


@router.post("/auth/verify")
def verify_auth(owner_id: str = Depends(get_current_user)):
    """Verify token validity and return user info."""
    return {"ok": True, "data": {"owner_id": owner_id}, "message": ""}
```

- [ ] **Step 2: Implement items CRUD**

Create `plugins/pendo/web/api/items.py`:

```python
"""Unified items CRUD API."""
import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from ...services.db import Database
from ...models.item import ItemType
from ..deps import get_db, get_current_user

router = APIRouter()


class ItemCreate(BaseModel):
    type: str
    title: str = ""
    content: str = ""
    tags: list[str] = []
    category: str = "未分类"
    # Event fields
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    location: Optional[str] = None
    timezone: Optional[str] = None
    remind_times: Optional[list[str]] = None
    rrule: Optional[str] = None
    notes: Optional[str] = None
    # Task fields
    due_time: Optional[str] = None
    priority: Optional[int] = None
    status: Optional[str] = None
    # Diary fields
    diary_date: Optional[str] = None
    mood: Optional[str] = None
    mood_score: Optional[int] = None
    weather: Optional[str] = None
    template_id: Optional[str] = None
    # Ledger fields
    amount: Optional[float] = None
    direction: Optional[str] = None
    ledger_category: Optional[str] = None
    ledger_date: Optional[str] = None
    remark: Optional[str] = None


class ItemUpdate(BaseModel):
    title: Optional[str] = None
    content: Optional[str] = None
    tags: Optional[list[str]] = None
    category: Optional[str] = None
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    location: Optional[str] = None
    timezone: Optional[str] = None
    remind_times: Optional[list[str]] = None
    rrule: Optional[str] = None
    notes: Optional[str] = None
    due_time: Optional[str] = None
    priority: Optional[int] = None
    status: Optional[str] = None
    diary_date: Optional[str] = None
    mood: Optional[str] = None
    mood_score: Optional[int] = None
    weather: Optional[str] = None
    template_id: Optional[str] = None
    amount: Optional[float] = None
    direction: Optional[str] = None
    ledger_category: Optional[str] = None
    ledger_date: Optional[str] = None
    remark: Optional[str] = None


def _item_to_dict(item) -> dict:
    """Convert Item dataclass to API response dict."""
    if hasattr(item, "to_dict"):
        return item.to_dict()
    return dict(item) if hasattr(item, "__iter__") else {}


@router.get("/items")
def list_items(
    type: Optional[str] = None,
    status: Optional[str] = None,
    category: Optional[str] = None,
    priority: Optional[int] = None,
    direction: Optional[str] = None,
    range: Optional[str] = Query(None, alias="range"),
    sort: str = "created_at",
    order: str = "desc",
    page: int = 1,
    page_size: int = 20,
    owner_id: str = Depends(get_current_user),
    db: Database = Depends(get_db),
):
    """List items with filtering and pagination."""
    filters = {}
    if type:
        filters["type"] = type
    if status:
        filters["status"] = status
    if category:
        filters["category"] = category
    if priority is not None:
        filters["priority"] = priority
    if direction:
        filters["direction"] = direction

    # Parse date range: "2026-03-01..2026-03-31"
    if range:
        parts = range.split("..")
        if len(parts) == 2:
            date_field = "created_at"
            if type == "event":
                date_field = "start_time"
            elif type == "task":
                date_field = "due_time"
            elif type == "diary":
                date_field = "diary_date"
            elif type == "ledger":
                date_field = "ledger_date"
            filters["date_field"] = date_field
            filters["start_date"] = parts[0]
            filters["end_date"] = parts[1]

    offset = (page - 1) * page_size
    items = db.get_items(owner_id, filters=filters, limit=page_size, offset=offset)

    # Post-filter for fields not supported by get_items() directly
    if direction:
        items = [i for i in items if getattr(i, "direction", None) == direction]
    if priority is not None:
        items = [i for i in items if getattr(i, "priority", None) == priority]

    # Get total count via COUNT query for pagination
    conn = db.get_connection()
    count_where = ["owner_id = ?", "deleted = 0"]
    count_params = [owner_id]
    if type:
        count_where.append("type = ?")
        count_params.append(type)
    if status:
        count_where.append("status = ?")
        count_params.append(status)
    if category:
        count_where.append("category = ?")
        count_params.append(category)
    total = conn.execute(
        f"SELECT COUNT(*) FROM items WHERE {' AND '.join(count_where)}",
        count_params,
    ).fetchone()[0]

    return {
        "ok": True,
        "data": [_item_to_dict(item) for item in items],
        "total": total,
        "message": "",
    }


@router.get("/items/{item_id}")
def get_item(
    item_id: str,
    owner_id: str = Depends(get_current_user),
    db: Database = Depends(get_db),
):
    """Get single item by ID."""
    item = db.get_item(item_id, owner_id=owner_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    return {"ok": True, "data": _item_to_dict(item), "message": ""}


@router.post("/items", status_code=201)
def create_item(
    body: ItemCreate,
    owner_id: str = Depends(get_current_user),
    db: Database = Depends(get_db),
):
    """Create a new item."""
    # Validate type
    try:
        ItemType(body.type)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Invalid type: {body.type}")

    now = datetime.now().isoformat()
    item_data = {
        "type": body.type,
        "title": body.title,
        "content": body.content,
        "tags": body.tags,
        "category": body.category,
        "owner_id": owner_id,
        "created_at": now,
        "updated_at": now,
        "context": {},
        "deleted": False,
    }

    # Add type-specific fields (only non-None)
    for field in body.model_fields:
        if field in ("type", "title", "content", "tags", "category"):
            continue
        value = getattr(body, field)
        if value is not None:
            item_data[field] = value

    # Default ledger_date to today if not set
    if body.type == "ledger" and not body.ledger_date:
        item_data["ledger_date"] = datetime.now().strftime("%Y-%m-%d")

    # Default task status
    if body.type == "task" and not body.status:
        item_data["status"] = "todo"
    if body.type == "task" and not body.priority:
        item_data["priority"] = 3

    item_id = db.insert_item(item_data)
    db.log_operation(owner_id, "create", item_type=body.type, item_id=item_id)

    return {"ok": True, "data": {"id": item_id}, "message": "创建成功"}


@router.put("/items/{item_id}")
def update_item(
    item_id: str,
    body: ItemUpdate,
    owner_id: str = Depends(get_current_user),
    db: Database = Depends(get_db),
):
    """Update an item."""
    item = db.get_item(item_id, owner_id=owner_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")

    updates = {}
    for field, value in body.model_dump(exclude_none=True).items():
        updates[field] = value

    if not updates:
        raise HTTPException(status_code=422, detail="No fields to update")

    updates["updated_at"] = datetime.now().isoformat()
    success = db.update_item(item_id, updates, owner_id=owner_id)
    if not success:
        raise HTTPException(status_code=500, detail="Update failed")

    db.log_operation(owner_id, "update", item_id=item_id)
    return {"ok": True, "data": {"id": item_id}, "message": "更新成功"}


@router.delete("/items/{item_id}")
def delete_item(
    item_id: str,
    owner_id: str = Depends(get_current_user),
    db: Database = Depends(get_db),
):
    """Soft delete an item."""
    item = db.get_item(item_id, owner_id=owner_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")

    success = db.delete_item(item_id, soft=True, owner_id=owner_id)
    if not success:
        raise HTTPException(status_code=500, detail="Delete failed")

    db.log_operation(owner_id, "delete", item_id=item_id)
    return {"ok": True, "data": {"id": item_id}, "message": "已删除"}
```

- [ ] **Step 3: Implement dashboard endpoint**

Create `plugins/pendo/web/api/dashboard.py`:

```python
"""Dashboard aggregation endpoint."""
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends

from ...services.db import Database
from ..deps import get_db, get_current_user

router = APIRouter()


@router.get("/dashboard")
def get_dashboard(
    owner_id: str = Depends(get_current_user),
    db: Database = Depends(get_db),
):
    """Get dashboard overview data."""
    today = datetime.now().strftime("%Y-%m-%d")
    today_start = f"{today}T00:00:00"
    today_end = f"{today}T23:59:59"
    month_start = datetime.now().strftime("%Y-%m-01")
    week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    month_ago = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")

    # Today's events
    events = db.get_items(owner_id, filters={
        "type": "event",
        "date_field": "start_time",
        "start_date": today_start,
        "end_date": today_end,
    }, limit=50)

    # Pending tasks (high priority + overdue first)
    tasks = db.get_items(owner_id, filters={
        "type": "task",
        "status": "todo",
    }, limit=20)
    tasks_in_progress = db.get_items(owner_id, filters={
        "type": "task",
        "status": "in_progress",
    }, limit=20)

    # Recent ledger entries
    recent_ledger = db.get_items(owner_id, filters={
        "type": "ledger",
        "date_field": "ledger_date",
        "start_date": week_ago,
        "end_date": today,
    }, limit=20)

    # Monthly spending trend (aggregate by day)
    month_ledger = db.get_items(owner_id, filters={
        "type": "ledger",
        "date_field": "ledger_date",
        "start_date": month_start,
        "end_date": today,
    }, limit=500)

    # Build spending trend
    spending_by_day = {}
    month_income = 0.0
    month_expense = 0.0
    for item in month_ledger:
        d = getattr(item, "ledger_date", None) or today
        amt = getattr(item, "amount", 0) or 0
        direction = getattr(item, "direction", "expense")
        if direction == "expense":
            spending_by_day[d] = spending_by_day.get(d, 0) + amt
            month_expense += amt
        else:
            month_income += amt

    spending_trend = [{"date": k, "amount": v} for k, v in sorted(spending_by_day.items())]

    # Summary counts
    all_events_today = len(events)
    all_tasks_pending = len(tasks) + len(tasks_in_progress)

    conn = db.get_connection()
    recent_ledger_count = conn.execute(
        "SELECT COUNT(*) FROM items WHERE type='ledger' AND owner_id=? AND deleted=0 AND ledger_date BETWEEN ? AND ?",
        (owner_id, week_ago, today),
    ).fetchone()[0]

    recent_diary_count = conn.execute(
        "SELECT COUNT(*) FROM items WHERE type='diary' AND owner_id=? AND deleted=0 AND diary_date BETWEEN ? AND ?",
        (owner_id, month_ago, today),
    ).fetchone()[0]

    def to_dict(item):
        return item.to_dict() if hasattr(item, "to_dict") else {}

    return {
        "ok": True,
        "data": {
            "summary": {
                "events_today": all_events_today,
                "tasks_pending": all_tasks_pending,
                "ledger_week": recent_ledger_count,
                "diary_month": recent_diary_count,
            },
            "events": [to_dict(e) for e in events],
            "tasks": [to_dict(t) for t in (tasks + tasks_in_progress)],
            "recent_ledger": [to_dict(l) for l in recent_ledger[:10]],
            "spending_trend": spending_trend,
            "month_summary": {
                "income": month_income,
                "expense": month_expense,
                "balance": month_income - month_expense,
            },
        },
        "message": "",
    }
```

- [ ] **Step 4: Commit**

```bash
git add plugins/pendo/web/api/auth_routes.py plugins/pendo/web/api/items.py plugins/pendo/web/api/dashboard.py
git commit -m "feat(pendo-web): add auth, items CRUD, and dashboard API endpoints"
```

---

### Task 6: API Endpoints - Search, Stats, Settings, Config

**Files:**
- Create: `plugins/pendo/web/api/search.py`
- Create: `plugins/pendo/web/api/stats.py`
- Create: `plugins/pendo/web/api/settings.py`
- Create: `plugins/pendo/web/api/config_routes.py`

- [ ] **Step 1: Implement search endpoint**

Create `plugins/pendo/web/api/search.py`:

```python
"""Search endpoint."""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from ...services.db import Database
from ..deps import get_db, get_current_user

router = APIRouter()


@router.get("/search")
def search_items(
    q: str,
    type: Optional[str] = None,
    category: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 50,
    owner_id: str = Depends(get_current_user),
    db: Database = Depends(get_db),
):
    """Full-text search across all item types."""
    if not q.strip():
        raise HTTPException(status_code=422, detail="Search query cannot be empty")

    filters = {}
    if type:
        filters["type"] = type
    if category:
        filters["category"] = category
    if status:
        filters["status"] = status

    results = db.search_items(owner_id, q.strip(), filters=filters, limit=limit)

    def to_dict(item):
        return item.to_dict() if hasattr(item, "to_dict") else {}

    return {
        "ok": True,
        "data": [to_dict(item) for item in results],
        "total": len(results),
        "message": "",
    }
```

- [ ] **Step 2: Implement stats endpoint**

Create `plugins/pendo/web/api/stats.py`:

```python
"""Statistics aggregation endpoints."""
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query

from ...services.db import Database
from ..deps import get_db, get_current_user

router = APIRouter()


def _parse_range(range_str: str | None) -> tuple[str, str]:
    """Parse range string into (start, end) dates."""
    now = datetime.now()
    if not range_str or range_str == "month":
        start = now.strftime("%Y-%m-01")
        end = now.strftime("%Y-%m-%d")
    elif range_str == "week":
        start = (now - timedelta(days=now.weekday())).strftime("%Y-%m-%d")
        end = now.strftime("%Y-%m-%d")
    elif range_str == "quarter":
        q_month = ((now.month - 1) // 3) * 3 + 1
        start = f"{now.year}-{q_month:02d}-01"
        end = now.strftime("%Y-%m-%d")
    elif range_str == "year":
        start = f"{now.year}-01-01"
        end = now.strftime("%Y-%m-%d")
    elif ".." in range_str:
        parts = range_str.split("..")
        start, end = parts[0], parts[1]
    else:
        start = range_str + "-01"
        end = range_str + "-31"
    return start, end


@router.get("/stats/ledger")
def ledger_stats(
    range: str | None = Query(None, alias="range"),
    owner_id: str = Depends(get_current_user),
    db: Database = Depends(get_db),
):
    """Ledger statistics: monthly comparison, category breakdown, daily trend."""
    start, end = _parse_range(range)
    conn = db.get_connection()

    # Monthly income/expense comparison
    monthly = conn.execute("""
        SELECT strftime('%Y-%m', ledger_date) AS month, direction, SUM(amount) AS total
        FROM items WHERE type='ledger' AND owner_id=? AND deleted=0
        AND ledger_date BETWEEN ? AND ?
        GROUP BY month, direction ORDER BY month
    """, (owner_id, start, end)).fetchall()

    # Category breakdown
    by_category = conn.execute("""
        SELECT ledger_category, direction, SUM(amount) AS total, COUNT(*) AS count
        FROM items WHERE type='ledger' AND owner_id=? AND deleted=0
        AND ledger_date BETWEEN ? AND ?
        GROUP BY ledger_category, direction
    """, (owner_id, start, end)).fetchall()

    # Daily spending trend
    daily = conn.execute("""
        SELECT ledger_date, direction, SUM(amount) AS total
        FROM items WHERE type='ledger' AND owner_id=? AND deleted=0
        AND ledger_date BETWEEN ? AND ?
        GROUP BY ledger_date, direction ORDER BY ledger_date
    """, (owner_id, start, end)).fetchall()

    return {
        "ok": True,
        "data": {
            "monthly": [{"month": r[0], "direction": r[1], "total": r[2]} for r in monthly],
            "by_category": [{"category": r[0], "direction": r[1], "total": r[2], "count": r[3]} for r in by_category],
            "daily": [{"date": r[0], "direction": r[1], "total": r[2]} for r in daily],
        },
        "message": "",
    }


@router.get("/stats/tasks")
def task_stats(
    range: str | None = Query(None, alias="range"),
    owner_id: str = Depends(get_current_user),
    db: Database = Depends(get_db),
):
    """Task statistics: completion rate, category/priority distribution."""
    start, end = _parse_range(range)
    conn = db.get_connection()

    # Overall counts
    totals = conn.execute("""
        SELECT status, COUNT(*) AS count
        FROM items WHERE type='task' AND owner_id=? AND deleted=0
        GROUP BY status
    """, (owner_id,)).fetchall()

    # Weekly completion rate
    weekly = conn.execute("""
        SELECT strftime('%Y-W%W', created_at) AS week,
            COUNT(*) AS total,
            SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) AS done
        FROM items WHERE type='task' AND owner_id=? AND deleted=0
        AND created_at BETWEEN ? AND ?
        GROUP BY week ORDER BY week
    """, (owner_id, start, end)).fetchall()

    # Category distribution
    by_category = conn.execute("""
        SELECT category, COUNT(*) AS count
        FROM items WHERE type='task' AND owner_id=? AND deleted=0
        GROUP BY category
    """, (owner_id,)).fetchall()

    # Priority distribution
    by_priority = conn.execute("""
        SELECT priority, COUNT(*) AS count
        FROM items WHERE type='task' AND owner_id=? AND deleted=0
        GROUP BY priority
    """, (owner_id,)).fetchall()

    # New tasks this week
    week_start = (datetime.now() - timedelta(days=datetime.now().weekday())).strftime("%Y-%m-%d")
    new_this_week = conn.execute("""
        SELECT COUNT(*) FROM items
        WHERE type='task' AND owner_id=? AND deleted=0 AND created_at >= ?
    """, (owner_id, week_start)).fetchone()[0]

    return {
        "ok": True,
        "data": {
            "totals": {r[0]: r[1] for r in totals},
            "weekly": [{"week": r[0], "total": r[1], "done": r[2]} for r in weekly],
            "by_category": [{"category": r[0], "count": r[1]} for r in by_category],
            "by_priority": [{"priority": r[0], "count": r[1]} for r in by_priority],
            "new_this_week": new_this_week,
        },
        "message": "",
    }


@router.get("/stats/events")
def event_stats(
    range: str | None = Query(None, alias="range"),
    owner_id: str = Depends(get_current_user),
    db: Database = Depends(get_db),
):
    """Event statistics: weekly busyness, time slot distribution, category."""
    start, end = _parse_range(range)
    conn = db.get_connection()

    # Weekly busyness
    weekly = conn.execute("""
        SELECT strftime('%Y-W%W', start_time) AS week, COUNT(*) AS count
        FROM items WHERE type='event' AND owner_id=? AND deleted=0
        AND start_time BETWEEN ? AND ?
        GROUP BY week ORDER BY week
    """, (owner_id, start, end)).fetchall()

    # Time slot distribution
    time_slots = conn.execute("""
        SELECT CASE
            WHEN CAST(strftime('%H', start_time) AS INT) BETWEEN 6 AND 8 THEN '06-09'
            WHEN CAST(strftime('%H', start_time) AS INT) BETWEEN 9 AND 11 THEN '09-12'
            WHEN CAST(strftime('%H', start_time) AS INT) BETWEEN 12 AND 13 THEN '12-14'
            WHEN CAST(strftime('%H', start_time) AS INT) BETWEEN 14 AND 17 THEN '14-18'
            WHEN CAST(strftime('%H', start_time) AS INT) BETWEEN 18 AND 20 THEN '18-21'
            ELSE '21-24'
        END AS time_slot, COUNT(*) AS count
        FROM items WHERE type='event' AND owner_id=? AND deleted=0
        AND start_time IS NOT NULL
        GROUP BY time_slot ORDER BY time_slot
    """, (owner_id,)).fetchall()

    # Category distribution
    by_category = conn.execute("""
        SELECT category, COUNT(*) AS count
        FROM items WHERE type='event' AND owner_id=? AND deleted=0
        GROUP BY category
    """, (owner_id,)).fetchall()

    return {
        "ok": True,
        "data": {
            "weekly": [{"week": r[0], "count": r[1]} for r in weekly],
            "time_slots": [{"slot": r[0], "count": r[1]} for r in time_slots],
            "by_category": [{"category": r[0], "count": r[1]} for r in by_category],
        },
        "message": "",
    }
```

- [ ] **Step 3: Implement settings endpoint**

Create `plugins/pendo/web/api/settings.py`:

```python
"""User settings endpoints."""
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from typing import Optional

from ...services.db import Database
from ..deps import get_db, get_current_user

router = APIRouter()


class SettingsUpdate(BaseModel):
    timezone: Optional[str] = None
    quiet_hours_start: Optional[str] = None
    quiet_hours_end: Optional[str] = None
    daily_report_time: Optional[str] = None
    diary_remind_time: Optional[str] = None
    default_category: Optional[str] = None
    settings_json: Optional[dict] = None


@router.get("/settings")
def get_settings(
    owner_id: str = Depends(get_current_user),
    db: Database = Depends(get_db),
):
    """Get user settings."""
    settings = db.get_user_settings(owner_id)
    return {"ok": True, "data": settings, "message": ""}


@router.put("/settings")
def update_settings(
    body: SettingsUpdate,
    owner_id: str = Depends(get_current_user),
    db: Database = Depends(get_db),
):
    """Update user settings."""
    updates = body.model_dump(exclude_none=True)
    if updates:
        db.update_user_settings(owner_id, updates)
    settings = db.get_user_settings(owner_id)
    return {"ok": True, "data": settings, "message": "设置已更新"}
```

- [ ] **Step 4: Implement config endpoints**

Create `plugins/pendo/web/api/config_routes.py`:

```python
"""Configuration data endpoints (categories, templates)."""
from fastapi import APIRouter

from ...config import PendoConfig, LEDGER_EXPENSE_CATEGORIES, LEDGER_INCOME_CATEGORIES, DIARY_TEMPLATES

router = APIRouter()


@router.get("/config/categories")
def get_categories():
    """Get available categories for all modules."""
    return {
        "ok": True,
        "data": {
            "ledger_expense": LEDGER_EXPENSE_CATEGORIES,
            "ledger_income": LEDGER_INCOME_CATEGORIES,
        },
        "message": "",
    }


@router.get("/diary/templates")
def get_diary_templates():
    """Get available diary templates."""
    templates = []
    for tid, tdata in DIARY_TEMPLATES.items():
        templates.append({
            "id": tid,
            "name": tdata.get("name", tid),
            "description": tdata.get("description", ""),
            "prompts": tdata.get("prompts", []),
        })
    return {"ok": True, "data": templates, "message": ""}
```

- [ ] **Step 5: Commit**

```bash
git add plugins/pendo/web/api/search.py plugins/pendo/web/api/stats.py plugins/pendo/web/api/settings.py plugins/pendo/web/api/config_routes.py
git commit -m "feat(pendo-web): add search, stats, settings, and config API endpoints"
```

---

### Task 7: Chat Command Integration

**Files:**
- Create: `plugins/pendo/handlers/web.py`
- Modify: `plugins/pendo/core/router.py`
- Modify: `plugins/pendo/main.py`

- [ ] **Step 1: Create web handler**

Create `plugins/pendo/handlers/web.py`:

```python
"""Handler for /pendo web commands."""
from ..config import PendoConfig
from ..web.auth import generate_token
from ..web import server as web_server
from ..utils.error_handlers import handle_command_errors


class WebHandler:
    def __init__(self, db):
        self.db = db

    @handle_command_errors
    async def handle(self, user_id: str, args: str, context=None, group_id=None):
        """Handle /pendo web subcommands."""
        parts = args.strip().split(maxsplit=1)
        subcmd = parts[0].lower() if parts else ""

        if subcmd == "token":
            return await self._generate_token(user_id)
        elif subcmd == "start":
            return await self._start(user_id, context)
        elif subcmd == "stop":
            return await self._stop(user_id, context)
        elif subcmd == "status":
            return await self._status(user_id, context)
        else:
            return self._help()

    async def _generate_token(self, user_id: str):
        """Generate a login token for the user."""
        token = generate_token(user_id, expires_hours=PendoConfig.WEB_TOKEN_EXPIRE_HOURS)
        url = web_server.get_url()
        running = web_server.is_running()
        status_text = "运行中" if running else "未启动"
        return {
            "status": "success",
            "message": (
                f"🔑 Web UI 登录 Token（{PendoConfig.WEB_TOKEN_EXPIRE_HOURS}小时有效）:\n\n"
                f"`{token}`\n\n"
                f"Web 服务状态: {status_text}\n"
                f"地址: {url}"
            ),
        }

    async def _start(self, user_id: str, context):
        """Start the web server (admin only)."""
        # TODO: Check admin permission via context
        if web_server.is_running():
            return {"status": "success", "message": f"⚡ Web UI 已在运行: {web_server.get_url()}"}
        started = web_server.start(self.db)
        if started:
            return {"status": "success", "message": f"✅ Web UI 已启动: {web_server.get_url()}"}
        return {"status": "error", "message": "❌ Web UI 启动失败"}

    async def _stop(self, user_id: str, context):
        """Stop the web server (admin only)."""
        # TODO: Check admin permission via context
        if not web_server.is_running():
            return {"status": "success", "message": "Web UI 未在运行"}
        stopped = web_server.stop()
        if stopped:
            return {"status": "success", "message": "✅ Web UI 已停止"}
        return {"status": "error", "message": "❌ Web UI 停止失败"}

    async def _status(self, user_id: str, context):
        """Show web server status (admin only)."""
        # TODO: Check admin permission via context
        running = web_server.is_running()
        status = "🟢 运行中" if running else "🔴 未启动"
        return {
            "status": "success",
            "message": f"Web UI 状态: {status}\n地址: {web_server.get_url()}\n端口: {PendoConfig.WEB_PORT}",
        }

    def _help(self):
        return {
            "status": "success",
            "message": (
                "📡 Web UI 管理:\n"
                "  /pendo web token  - 生成登录 Token\n"
                "  /pendo web start  - 启动 Web 服务\n"
                "  /pendo web stop   - 停止 Web 服务\n"
                "  /pendo web status - 查看服务状态"
            ),
        }
```

- [ ] **Step 2: Add "web" to COMMAND_META in router.py**

In `plugins/pendo/core/router.py`, add to `COMMAND_META` dict:

```python
    "web": (["webui", "网页"], "Web UI 管理", "/pendo web <token|start|stop|status>"),
```

- [ ] **Step 3: Wire up web handler in main.py**

In `plugins/pendo/main.py`:

**a) Add import at top with other handler imports:**
```python
from .handlers.web import WebHandler
```

**b) In `_get_services()`, add after other handler instantiations:**
```python
services["web_handler"] = WebHandler(db)
```

**c) In `_build_command_router()` (~line 456), add `"web"` to the handlers dict:**
```python
    handlers = {
        "event": _help_or_exec(event_handler.handle, "event"),
        # ... existing entries ...
        "undo": _undo_cmd,
        "web": _help_or_exec(services["web_handler"].handle, "web"),  # ADD THIS LINE
    }
```

Note: `services["web_handler"]` must be accessible in scope. If `_build_command_router` does not receive the services dict, pass `web_handler` as a parameter or access it via the services dict directly.

**d) In `init()`, add auto-start after services are created:**
```python
if PendoConfig.WEB_ENABLED:
    try:
        from .web import server as web_server
        web_server.start(db)
    except Exception as e:
        logger.warning("Failed to auto-start web UI: %s", e)
```

**e) In `cleanup()`, add stop logic:**
```python
try:
    from .web import server as web_server
    web_server.stop()
except Exception:
    pass
```

**f) After Task 7, run existing pendo tests to verify no regressions:**
```bash
python -m pytest tests/plugins/test_pendo.py -v
```

- [ ] **Step 4: Commit**

```bash
git add plugins/pendo/handlers/web.py plugins/pendo/core/router.py plugins/pendo/main.py
git commit -m "feat(pendo-web): add chat command integration for web UI management"
```

---

## Phase 2: Frontend Core

### Task 8: HTML Shell & CSS

**Files:**
- Create: `plugins/pendo/web/static/index.html`
- Create: `plugins/pendo/web/static/css/app.css`
- Create: `plugins/pendo/web/static/css/charts.css`

- [ ] **Step 1: Create index.html**

Create `plugins/pendo/web/static/index.html` — the SPA shell:

```html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Pendo - 个人管理</title>
    <link rel="stylesheet" href="/css/app.css">
    <link rel="stylesheet" href="/css/charts.css">
</head>
<body>
    <!-- Login screen -->
    <div id="login-screen" class="login-screen" style="display: none;">
        <div class="login-card">
            <h1>📋 Pendo</h1>
            <p>请输入登录 Token</p>
            <input type="text" id="token-input" placeholder="粘贴 Token..." autocomplete="off">
            <button id="login-btn" class="btn btn-primary">登录</button>
            <p id="login-error" class="error-text" style="display: none;"></p>
        </div>
    </div>

    <!-- Main app -->
    <div id="app" style="display: none;">
        <div id="sidebar-container"></div>
        <div class="main-wrapper">
            <div id="header-container"></div>
            <main id="content" class="content"></main>
        </div>
        <div id="fab-container"></div>
    </div>

    <!-- Toast container -->
    <div id="toast-container" class="toast-container"></div>

    <!-- Modal container -->
    <div id="modal-overlay" class="modal-overlay" style="display: none;">
        <div id="modal-content" class="modal-content"></div>
    </div>

    <script type="module" src="/js/app.js"></script>
</body>
</html>
```

- [ ] **Step 2: Create app.css**

Create `plugins/pendo/web/static/css/app.css` with:
- CSS variables (module colors, spacing, radius, shadows)
- Base reset & typography
- Layout grid (sidebar + main)
- Component styles: buttons, cards, forms, tables, tags, badges
- Sidebar styles
- Header styles
- Modal styles
- Toast styles
- FAB styles
- Login screen styles
- Responsive breakpoints (1024px, 768px)
- Utility classes

Key CSS variables:

```css
:root {
    --color-bg: #F9FAFB;
    --color-surface: #FFFFFF;
    --color-border: #E5E7EB;
    --color-text: #111827;
    --color-text-secondary: #6B7280;
    --color-dashboard: #6366F1;
    --color-events: #F59E0B;
    --color-tasks: #10B981;
    --color-ledger: #EF4444;
    --color-notes: #3B82F6;
    --color-diary: #EC4899;
    --color-search: #6B7280;
    --color-stats: #8B5CF6;
    --radius: 12px;
    --radius-sm: 8px;
    --shadow: 0 1px 3px rgba(0,0,0,0.1);
    --shadow-lg: 0 4px 12px rgba(0,0,0,0.15);
    --sidebar-width: 220px;
    --header-height: 56px;
}
```

(Full CSS will be ~400 lines covering all components. The implementation agent should write the complete stylesheet.)

- [ ] **Step 3: Create charts.css**

Create `plugins/pendo/web/static/css/charts.css`:

```css
.chart-container { position: relative; width: 100%; margin-bottom: 24px; }
.chart-container canvas { width: 100% !important; }
.chart-row { display: grid; grid-template-columns: 1fr 1fr; gap: 24px; }
.chart-card { background: var(--color-surface); border-radius: var(--radius); padding: 20px; box-shadow: var(--shadow); border: 1px solid var(--color-border); }
.chart-card h3 { margin: 0 0 16px; font-size: 16px; color: var(--color-text); }
@media (max-width: 768px) { .chart-row { grid-template-columns: 1fr; } }
```

- [ ] **Step 4: Commit**

```bash
git add plugins/pendo/web/static/
git commit -m "feat(pendo-web): add HTML shell and CSS styles"
```

---

### Task 9: JS Core — Router, API Client, Store

**Files:**
- Create: `plugins/pendo/web/static/js/router.js`
- Create: `plugins/pendo/web/static/js/api.js`
- Create: `plugins/pendo/web/static/js/store.js`
- Create: `plugins/pendo/web/static/js/app.js`

- [ ] **Step 1: Implement router.js**

Hash-based SPA router with parameter support. Each page module exports `render(container)`, `destroy()`, `onRouteEnter(params)`.

```javascript
// router.js
const routes = {};
let currentPage = null;
let contentEl = null;

export function registerRoute(path, loader) {
    routes[path] = loader;
}

export function navigate(path) {
    window.location.hash = '#/' + path;
}

export function getParams() {
    const hash = window.location.hash.slice(2) || '';
    const [path, query] = hash.split('?');
    const params = new URLSearchParams(query || '');
    return { path, params };
}

export async function init(container) {
    contentEl = container;
    window.addEventListener('hashchange', () => loadCurrentRoute());
    await loadCurrentRoute();
}

async function loadCurrentRoute() {
    const { path, params } = getParams();
    const routePath = path || 'dashboard';

    if (currentPage && currentPage.destroy) {
        currentPage.destroy();
    }

    const loader = routes[routePath];
    if (!loader) {
        contentEl.innerHTML = '<div class="empty-state">页面不存在</div>';
        return;
    }

    try {
        const page = await loader();
        currentPage = page;
        contentEl.innerHTML = '';
        if (page.onRouteEnter) page.onRouteEnter(params);
        if (page.render) page.render(contentEl);
    } catch (e) {
        contentEl.innerHTML = `<div class="error-state">加载失败: ${e.message}</div>`;
    }
}
```

- [ ] **Step 2: Implement api.js**

Fetch wrapper with Bearer token, error handling, 401 redirect.

```javascript
// api.js
const TOKEN_KEY = 'pendo_token';

export function getToken() { return localStorage.getItem(TOKEN_KEY); }
export function setToken(t) { localStorage.setItem(TOKEN_KEY, t); }
export function clearToken() { localStorage.removeItem(TOKEN_KEY); }

async function request(path, options = {}) {
    const token = getToken();
    const res = await fetch(`/api${path}`, {
        headers: {
            'Authorization': `Bearer ${token}`,
            'Content-Type': 'application/json',
            ...options.headers,
        },
        ...options,
    });
    if (res.status === 401) {
        clearToken();
        window.location.hash = '';
        window.location.reload();
        throw new Error('Unauthorized');
    }
    const data = await res.json();
    if (!data.ok) throw new Error(data.detail || data.message || 'Request failed');
    return data;
}

export const api = {
    get: (path, params) => {
        const qs = params ? '?' + new URLSearchParams(params).toString() : '';
        return request(path + qs);
    },
    post: (path, body) => request(path, { method: 'POST', body: JSON.stringify(body) }),
    put: (path, body) => request(path, { method: 'PUT', body: JSON.stringify(body) }),
    delete: (path) => request(path, { method: 'DELETE' }),
};

export async function verifyToken(token) {
    const res = await fetch('/api/auth/verify', {
        method: 'POST',
        headers: { 'Authorization': `Bearer ${token}`, 'Content-Type': 'application/json' },
    });
    return res.ok;
}
```

- [ ] **Step 3: Implement store.js**

Simple reactive state store.

```javascript
// store.js
const state = {
    user: null,
    currentPage: 'dashboard',
    categories: null,
    diaryTemplates: null,
};
const listeners = [];

export function getState() { return state; }
export function setState(updates) {
    Object.assign(state, updates);
    listeners.forEach(fn => fn(state));
}
export function subscribe(fn) {
    listeners.push(fn);
    return () => { const i = listeners.indexOf(fn); if (i >= 0) listeners.splice(i, 1); };
}
```

- [ ] **Step 4: Implement app.js**

Bootstrap: check token, show login or app, register routes, init router.

```javascript
// app.js
import { getToken, setToken, clearToken, verifyToken } from './api.js';
import { init as initRouter, registerRoute } from './router.js';
import { setState } from './store.js';

// Register all page routes (lazy loaded)
registerRoute('dashboard', () => import('./pages/dashboard.js'));
registerRoute('events', () => import('./pages/events.js'));
registerRoute('tasks', () => import('./pages/tasks.js'));
registerRoute('ledger', () => import('./pages/ledger.js'));
registerRoute('notes', () => import('./pages/notes.js'));
registerRoute('diary', () => import('./pages/diary.js'));
registerRoute('search', () => import('./pages/search.js'));
registerRoute('stats', () => import('./pages/stats.js'));
registerRoute('settings', () => import('./pages/settings.js'));

async function bootstrap() {
    const token = getToken();
    if (token) {
        const valid = await verifyToken(token);
        if (valid) {
            showApp();
            return;
        }
        clearToken();
    }
    showLogin();
}

function showLogin() {
    document.getElementById('login-screen').style.display = 'flex';
    document.getElementById('app').style.display = 'none';
    const btn = document.getElementById('login-btn');
    const input = document.getElementById('token-input');
    const error = document.getElementById('login-error');

    btn.onclick = async () => {
        const token = input.value.trim();
        if (!token) return;
        btn.disabled = true;
        error.style.display = 'none';
        const valid = await verifyToken(token);
        if (valid) {
            setToken(token);
            showApp();
        } else {
            error.textContent = 'Token 无效或已过期';
            error.style.display = 'block';
        }
        btn.disabled = false;
    };

    input.onkeydown = (e) => { if (e.key === 'Enter') btn.click(); };
}

async function showApp() {
    document.getElementById('login-screen').style.display = 'none';
    document.getElementById('app').style.display = 'flex';

    // Init components
    const { renderSidebar } = await import('./components/sidebar.js');
    const { renderHeader } = await import('./components/header.js');
    const { renderFab } = await import('./components/fab.js');

    renderSidebar(document.getElementById('sidebar-container'));
    renderHeader(document.getElementById('header-container'));
    renderFab(document.getElementById('fab-container'));

    // Init router
    await initRouter(document.getElementById('content'));
}

bootstrap();
```

- [ ] **Step 5: Commit**

```bash
git add plugins/pendo/web/static/js/
git commit -m "feat(pendo-web): add JS core - router, API client, store, app bootstrap"
```

---

### Task 10: Shared Components

**Files:**
- Create: `plugins/pendo/web/static/js/components/sidebar.js`
- Create: `plugins/pendo/web/static/js/components/header.js`
- Create: `plugins/pendo/web/static/js/components/modal.js`
- Create: `plugins/pendo/web/static/js/components/toast.js`
- Create: `plugins/pendo/web/static/js/components/form.js`
- Create: `plugins/pendo/web/static/js/components/pagination.js`
- Create: `plugins/pendo/web/static/js/components/fab.js`

- [ ] **Step 1: Implement sidebar.js**

Navigation sidebar with module icons and colors. Highlights active page. Collapsible on tablet. Hidden on mobile with hamburger menu.

```javascript
// sidebar.js
import { navigate, getParams } from '../router.js';

const NAV_ITEMS = [
    { path: 'dashboard', label: '总览', icon: '📊', color: 'var(--color-dashboard)' },
    { path: 'events',    label: '日程', icon: '🗓️', color: 'var(--color-events)' },
    { path: 'tasks',     label: '待办', icon: '✅', color: 'var(--color-tasks)' },
    { path: 'ledger',    label: '记账', icon: '💰', color: 'var(--color-ledger)' },
    { path: 'notes',     label: '笔记', icon: '📝', color: 'var(--color-notes)' },
    { path: 'diary',     label: '日记', icon: '📔', color: 'var(--color-diary)' },
    { path: 'search',    label: '搜索', icon: '🔍', color: 'var(--color-search)' },
    { path: 'stats',     label: '统计', icon: '📈', color: 'var(--color-stats)' },
];
const BOTTOM_ITEMS = [
    { path: 'settings', label: '设置', icon: '⚙️', color: 'var(--color-text-secondary)' },
];

export function renderSidebar(container) {
    // Build sidebar HTML with nav items, highlight active, listen to hashchange
    // Implementation: create nav element, iterate NAV_ITEMS + BOTTOM_ITEMS
    // Each item: <a class="nav-item [active]" href="#/{path}">icon label</a>
    // Update active on hashchange
}
```

(Each component follows this pattern — export a `render(container)` function. Full implementations will be written by the implementation agent.)

- [ ] **Step 2: Implement header.js**

Top bar with page title (updates on route change), search input (navigates to `#/search?q=...`), user info, logout button.

- [ ] **Step 3: Implement modal.js**

Reusable modal: `showModal(title, contentHTML, options)`, `closeModal()`. Supports form submission callback.

- [ ] **Step 4: Implement toast.js**

Toast notifications: `showToast(message, type, duration)`. Types: success, error, info. Auto-dismiss. Supports undo callback.

- [ ] **Step 5: Implement form.js**

Form builder helpers: `buildForm(fields)` generates form HTML from field definitions. Handles validation, datetime-local, select, tag input.

- [ ] **Step 6: Implement pagination.js**

Pagination controls: `renderPagination(container, { page, pageSize, total, onChange })`.

- [ ] **Step 7: Implement fab.js**

Floating action button: click to expand 5 quick-add options. Each opens the appropriate modal form.

- [ ] **Step 8: Commit**

```bash
git add plugins/pendo/web/static/js/components/
git commit -m "feat(pendo-web): add shared UI components"
```

---

## Phase 3: Frontend Pages

### Task 11: Dashboard Page

**Files:**
- Create: `plugins/pendo/web/static/js/pages/dashboard.js`

- [ ] **Step 1: Implement dashboard page**

Calls `GET /api/dashboard`. Renders:
1. Four summary cards (events today, tasks pending, ledger week, diary month)
2. Today's events timeline (sorted by start_time)
3. Pending tasks list (priority-sorted, checkbox to mark done)
4. Mini spending trend chart (Chart.js line, no axis labels)
5. Month income/expense/balance summary

Uses module colors for each section. Mini chart links to `#/stats`.

- [ ] **Step 2: Commit**

```bash
git add plugins/pendo/web/static/js/pages/dashboard.js
git commit -m "feat(pendo-web): add dashboard page"
```

---

### Task 12: Events Page

**Files:**
- Create: `plugins/pendo/web/static/js/pages/events.js`

- [ ] **Step 1: Implement events page**

Calendar view (month grid) + list view toggle. Features:
- Month navigation (prev/next)
- Day cells show dot indicators for days with events
- Click day to show events for that day below calendar
- Click empty day to open add modal (pre-filled date)
- List view: filterable by date range
- Add/edit modal with all event fields (from spec Section 3)
- Delete with confirmation

- [ ] **Step 2: Commit**

```bash
git add plugins/pendo/web/static/js/pages/events.js
git commit -m "feat(pendo-web): add events page with calendar view"
```

---

### Task 13: Tasks Page

**Files:**
- Create: `plugins/pendo/web/static/js/pages/tasks.js`

- [ ] **Step 1: Implement tasks page**

Kanban board with 4 columns: TODO, In Progress, Done, Cancelled (collapsed).
- Cards show: priority color, title, category tag, due date
- Drag & drop between columns (native HTML5 DnD)
- Click card to open edit modal
- Add button at bottom of TODO column
- Filter by category, priority

- [ ] **Step 2: Commit**

```bash
git add plugins/pendo/web/static/js/pages/tasks.js
git commit -m "feat(pendo-web): add tasks page with kanban view"
```

---

### Task 14: Ledger Page

**Files:**
- Create: `plugins/pendo/web/static/js/pages/ledger.js`

- [ ] **Step 1: Implement ledger page**

Top: 3 summary cards (income/expense/balance for current filter range).
Quick-add form inline: direction select, amount, title (maps to summary), category select, submit.
List: grouped by date, each row shows icon + title + category + amount (green for income, red for expense).
Filter: date range selector, direction, category.
Edit/delete via row actions.

- [ ] **Step 2: Commit**

```bash
git add plugins/pendo/web/static/js/pages/ledger.js
git commit -m "feat(pendo-web): add ledger page with quick-add form"
```

---

### Task 15: Notes Page

**Files:**
- Create: `plugins/pendo/web/static/js/pages/notes.js`

- [ ] **Step 1: Implement notes page**

Card grid layout (CSS grid, 3 columns desktop, 2 tablet, 1 mobile).
Each card: title, content preview (first 100 chars), tags, category badge, updated_at.
Filter: category dropdown, tag filter.
Click card → modal with full content (rendered Markdown-ish, or plain text).
Add/edit/delete modals.

- [ ] **Step 2: Commit**

```bash
git add plugins/pendo/web/static/js/pages/notes.js
git commit -m "feat(pendo-web): add notes page with card grid"
```

---

### Task 16: Diary Page

**Files:**
- Create: `plugins/pendo/web/static/js/pages/diary.js`

- [ ] **Step 1: Implement diary page**

Timeline layout (date headers, entries below).
Each entry: date, mood emoji, weather icon, location, content preview.
Filter by month.
Add: date picker (default today), template selector (fetches from `/api/diary/templates`), content textarea, mood/weather selectors.
View/edit/delete.

- [ ] **Step 2: Commit**

```bash
git add plugins/pendo/web/static/js/pages/diary.js
git commit -m "feat(pendo-web): add diary page with timeline view"
```

---

### Task 17: Search Page

**Files:**
- Create: `plugins/pendo/web/static/js/pages/search.js`

- [ ] **Step 1: Implement search page**

Top: search input (pre-filled from URL params `?q=`).
Type filter tabs: 全部 / 日程 / 待办 / 记账 / 笔记 / 日记.
Results: cards showing type icon + title + content preview + timestamp.
Click result → navigate to the item's module page or open detail modal.

- [ ] **Step 2: Commit**

```bash
git add plugins/pendo/web/static/js/pages/search.js
git commit -m "feat(pendo-web): add search page"
```

---

### Task 18: Stats Page

**Files:**
- Create: `plugins/pendo/web/static/js/pages/stats.js`
- Vendor: `plugins/pendo/web/static/js/lib/chart.min.js`

- [ ] **Step 1: Download Chart.js**

```bash
curl -o plugins/pendo/web/static/js/lib/chart.min.js https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js
```

- [ ] **Step 2: Implement stats page**

Tab navigation: 记账 | 待办 | 日程.
Date range selector: week/month/quarter/year/custom.

**Ledger tab:**
- Monthly income/expense bar chart (dual-color bars)
- Expense category pie chart (click to show detail)
- Income category pie chart
- Daily spending line chart

**Tasks tab:**
- Summary cards (total/done/rate/new this week)
- Weekly completion rate line chart
- Category pie chart
- Priority donut chart

**Events tab:**
- Weekly busyness bar chart
- Time slot horizontal bar chart
- Category pie chart

All charts use Chart.js. Each chart in a `.chart-card` container with title and optional PNG download button.

- [ ] **Step 3: Commit**

```bash
git add plugins/pendo/web/static/js/pages/stats.js plugins/pendo/web/static/js/lib/
git commit -m "feat(pendo-web): add stats page with Chart.js visualizations"
```

---

### Task 19: Settings Page

**Files:**
- Create: `plugins/pendo/web/static/js/pages/settings.js`

- [ ] **Step 1: Implement settings page**

Form layout with sections:
1. **时区**: dropdown (common timezones)
2. **静默时段**: start/end time inputs
3. **每日简报时间**: time input
4. **日记提醒时间**: time input
5. **默认分类**: text input
6. **开关项**: reminder, daily_report, privacy (toggle switches)
7. **Token 管理**: display current login status, link to get new token via chat

Save button calls `PUT /api/settings`. Toast on success.

- [ ] **Step 2: Commit**

```bash
git add plugins/pendo/web/static/js/pages/settings.js
git commit -m "feat(pendo-web): add settings page"
```

---

## Phase 4: Integration & Polish

### Task 20: End-to-End Smoke Test

- [ ] **Step 1: Start the plugin and web server**

Verify `init()` starts the web server on `http://127.0.0.1:8765`.

- [ ] **Step 2: Generate token via chat**

Send `/pendo web token` and get a valid JWT.

- [ ] **Step 3: Login via browser**

Open `http://127.0.0.1:8765`, paste token, verify login succeeds.

- [ ] **Step 4: Test each page**

Navigate through all pages: dashboard, events, tasks, ledger, notes, diary, search, stats, settings. Verify data loads and renders.

- [ ] **Step 5: Test CRUD operations**

Create, edit, and delete one item of each type via the web UI. Verify changes appear in both web UI and chat commands.

- [ ] **Step 6: Test stats charts**

Navigate to stats page, verify all charts render with data.

- [ ] **Step 7: Fix any issues found**

Address bugs discovered during smoke testing.

- [ ] **Step 8: Final commit**

```bash
git add -A
git commit -m "fix(pendo-web): address issues found in smoke testing"
```

---

## Summary

| Phase | Tasks | Description |
|-------|-------|-------------|
| 1 | 1-7 | Backend: config, auth, server, API endpoints, chat commands |
| 2 | 8-10 | Frontend core: HTML, CSS, JS router/api/store, components |
| 3 | 11-19 | Frontend pages: dashboard, events, tasks, ledger, notes, diary, search, stats, settings |
| 4 | 20 | Integration testing and polish |

Total: 20 tasks, ~60 steps.
