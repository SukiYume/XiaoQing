"""FastAPI web server for Pendo Web UI."""
import logging
import threading
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles

from ..config import PendoConfig
from ..services.db import Database
from .api import create_api_router
from .deps import set_db

logger = logging.getLogger("pendo.web")

_app: FastAPI | None = None
_server: uvicorn.Server | None = None
_thread: threading.Thread | None = None
_last_error: str | None = None

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

    @app.exception_handler(RequestValidationError)
    async def request_validation_exception_handler(request, exc):
        errors = [
            {
                "loc": err.get("loc", []),
                "msg": err.get("msg", "Invalid value"),
                "type": err.get("type", "value_error"),
            }
            for err in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content={
                "ok": False,
                "message": "请求参数校验失败",
                "error_code": "validation_error",
                "errors": errors,
            },
        )

    app.include_router(create_api_router(), prefix="/api")

    # Mount static files (SPA fallback to index.html)
    if STATIC_DIR.exists():
        app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")

    return app


def _format_start_error(host: str, port: int, exc: BaseException) -> str:
    """Format startup failures with lightweight, actionable diagnostics."""
    target = f"{host}:{port}"
    detail = str(exc)
    if isinstance(exc, OSError):
        winerror = getattr(exc, "winerror", None)
        errno = getattr(exc, "errno", None)

        if winerror == 10048:
            return f"无法绑定到 {target}，端口已被其他进程占用。原始错误: {detail}"

        if winerror == 10013 or errno == 13:
            return (
                f"无法绑定到 {target}，系统拒绝了这次套接字绑定（WinError 10013）。"
                "这通常与 Windows 保留端口、虚拟化网络组件或安全策略有关，"
                "可尝试改用其他端口。"
                f" 原始错误: {detail}"
            )

    return f"无法绑定到 {target}。原始错误: {detail}"


def _reset_state() -> None:
    """Clear server globals after a completed stop or failed startup."""
    global _app, _server, _thread

    _app = None
    _server = None
    _thread = None


def _run_server() -> None:
    """Run uvicorn and preserve startup failures for command-level reporting."""
    global _last_error

    if _server is None:
        _last_error = "Web server is not initialized"
        return

    try:
        _server.run()
    except Exception as exc:
        _last_error = _format_start_error(PendoConfig.WEB_HOST, PendoConfig.WEB_PORT, exc)
        logger.exception("Pendo Web UI crashed during startup")


def start(db: Database) -> bool:
    """Start the web server in a background thread.

    Returns True if started, False if already running.
    """
    global _app, _server, _thread, _last_error

    if _thread is not None and _thread.is_alive():
        return False

    _last_error = None
    _app = create_app(db)

    config = uvicorn.Config(
        _app,
        host=PendoConfig.WEB_HOST,
        port=PendoConfig.WEB_PORT,
        log_level="warning",
    )
    _server = uvicorn.Server(config)
    _thread = threading.Thread(target=_run_server, daemon=True, name="pendo-web")
    _thread.start()

    # Uvicorn starts in a background thread; wait briefly so bind failures become visible
    # to `/pendo web start` instead of surfacing only in logs.
    for _ in range(20):
        if getattr(_server, "started", False):
            logger.info("Pendo Web UI started on http://%s:%d", PendoConfig.WEB_HOST, PendoConfig.WEB_PORT)
            return True
        if not _thread.is_alive():
            break
        time.sleep(0.05)

    _last_error = _last_error or (
        f"Web UI 未能在 {PendoConfig.WEB_HOST}:{PendoConfig.WEB_PORT} 上完成启动"
    )
    logger.warning("Pendo Web UI startup failed: %s", _last_error)
    _reset_state()
    return False


def stop(timeout: float = 5.0) -> bool:
    """Stop the web server.

    Returns True if stopped, False if not running.
    """
    global _app, _server, _thread, _last_error

    if _server is None or _thread is None or not _thread.is_alive():
        return False

    _server.should_exit = True
    _thread.join(timeout=timeout)
    if _thread.is_alive() and hasattr(_server, "force_exit"):
        _server.force_exit = True
        _thread.join(timeout=1.0)

    if _thread.is_alive():
        logger.warning("Pendo Web UI did not stop within %.1fs", timeout + 1.0)
        return False

    _reset_state()
    _last_error = None
    logger.info("Pendo Web UI stopped")
    return True


def is_managed_running() -> bool:
    """Check whether this process owns a running web server thread."""
    return _thread is not None and _thread.is_alive()


def is_reachable(timeout: float = 0.3) -> bool:
    """Check whether a web service is reachable at the configured URL."""
    try:
        with urlopen(get_url(), timeout=timeout) as response:
            return 200 <= response.status < 500
    except (OSError, URLError, TimeoutError):
        return False


def is_running() -> bool:
    """Check if the web UI is running, including an externally started server."""
    return is_managed_running() or is_reachable()


def get_last_error() -> str | None:
    """Return the most recent startup failure for user-facing diagnostics."""
    return _last_error


def get_url() -> str:
    """Get the web UI URL."""
    return f"http://{PendoConfig.WEB_HOST}:{PendoConfig.WEB_PORT}"
