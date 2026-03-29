"""FastAPI web server for Pendo Web UI."""
import logging
import threading
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles

from ..config import PendoConfig
from ..services.db import Database
from .api import create_api_router
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
    from fastapi.responses import JSONResponse

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request, exc):
        return JSONResponse(
            status_code=exc.status_code,
            content={"ok": False, "message": exc.detail, "error_code": str(exc.status_code)},
        )

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
