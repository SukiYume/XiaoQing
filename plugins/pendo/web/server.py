"""FastAPI web server for Pendo Web UI.

模块级状态只代表当前进程实际拥有的一个 Uvicorn 线程；start/stop 必须在同一状态锁内
串行化。启动超时不能直接清空引用并遗留孤儿线程，只有确认线程退出后才能重置状态；
可达性探测仅用于展示外部服务状态，不能冒充本进程对服务生命周期的所有权。
"""

import logging
import math
import threading
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
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
_STATE_LOCK = threading.RLock()

STATIC_DIR = Path(__file__).parent / "static"
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}
_CSP = (
    "default-src 'self'; "
    "base-uri 'self'; "
    "object-src 'none'; "
    "frame-ancestors 'none'; "
    "form-action 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; "
    "connect-src 'self'"
)


def create_app(db: Database) -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(title="Pendo Web UI", docs_url=None, redoc_url=None)

    @app.middleware("http")
    async def add_security_headers(request, call_next):
        response = await call_next(request)
        response.headers.setdefault("Content-Security-Policy", _CSP)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        return response

    # 数据库依赖必须在挂载路由前绑定，路由处理期间只读取这一实例。
    set_db(db)

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
    if isinstance(exc, OSError):
        winerror = getattr(exc, "winerror", None)
        errno = getattr(exc, "errno", None)

        if winerror == 10048:
            return f"无法绑定到 {target}，端口已被其他进程占用。"

        if winerror == 10013 or errno == 13:
            return (
                f"无法绑定到 {target}，系统拒绝了这次套接字绑定（WinError 10013）。"
                "这通常与 Windows 保留端口、虚拟化网络组件或安全策略有关，"
                "可尝试改用其他端口。"
            )

    return f"无法绑定到 {target}（错误类型: {type(exc).__name__}）。"


def _reset_state() -> None:
    """Clear server globals after a completed stop or failed startup."""
    global _app, _server, _thread

    _app = None
    _server = None
    _thread = None


def _run_server(server: uvicorn.Server, host: str, port: int) -> None:
    """Run uvicorn and preserve startup failures for command-level reporting."""
    global _last_error

    try:
        server.run()
    except BaseException as exc:
        _last_error = _format_start_error(host, port, exc)
        logger.error(
            "Pendo Web UI crashed during startup error_type=%s",
            type(exc).__name__,
        )


def _validated_timeout(value: float, *, field_name: str) -> float:
    if isinstance(value, bool):
        raise TypeError(f"{field_name} must be a positive finite number")
    try:
        timeout = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise TypeError(f"{field_name} must be a positive finite number") from exc
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError(f"{field_name} must be a positive finite number")
    return timeout


def _request_thread_stop(
    server: uvicorn.Server,
    thread: threading.Thread,
    *,
    timeout: float,
) -> bool:
    server.should_exit = True
    thread.join(timeout=timeout)
    if thread.is_alive() and hasattr(server, "force_exit"):
        server.force_exit = True
        thread.join(timeout=min(1.0, timeout))
    return not thread.is_alive()


def start(db: Database) -> bool:
    """Start the web server in a background thread.

    Returns True if started, False if already running.
    """
    global _app, _server, _thread, _last_error

    with _STATE_LOCK:
        if _thread is not None and _thread.is_alive():
            return False

        runtime = PendoConfig.runtime()
        host = runtime.web_host
        port = runtime.web_port
        if host.lower() not in _LOOPBACK_HOSTS and not runtime.web_session_cookie_secure:
            _last_error = (
                "拒绝将 Pendo Web 绑定到非 loopback 地址而不使用 Secure session cookie；"
                "请在 TLS 反向代理后设置 "
                "plugins.pendo.web_session_cookie_secure=true。"
            )
            logger.error(_last_error)
            return False

        _last_error = None
        _app = create_app(db)
        config = uvicorn.Config(_app, host=host, port=port, log_level="warning")
        server = uvicorn.Server(config)
        thread = threading.Thread(
            target=_run_server,
            args=(server, host, port),
            daemon=True,
            name="pendo-web",
        )
        _server = server
        _thread = thread
        try:
            thread.start()
        except Exception as exc:
            _last_error = _format_start_error(host, port, exc)
            _reset_state()
            return False

        # 短暂等待让端口绑定错误直接返回给命令；超时后必须先终止并回收线程，
        # 不能清空全局引用后让它在后台继续完成启动。
        for _ in range(20):
            if getattr(server, "started", False):
                logger.info("Pendo Web UI started on http://%s:%d", host, port)
                return True
            if not thread.is_alive():
                break
            time.sleep(0.05)

        _last_error = _last_error or f"Web UI 未能在 {host}:{port} 上完成启动"
        logger.warning("Pendo Web UI startup failed: %s", _last_error)
        stopped = not thread.is_alive() or _request_thread_stop(server, thread, timeout=1.0)
        if stopped:
            _reset_state()
        return False


def stop(timeout: float = 5.0) -> bool:
    """Stop the web server.

    Returns True if stopped, False if not running.
    """
    global _app, _server, _thread, _last_error

    normalized_timeout = _validated_timeout(timeout, field_name="timeout")
    with _STATE_LOCK:
        if _server is None or _thread is None or not _thread.is_alive():
            return False

        if not _request_thread_stop(_server, _thread, timeout=normalized_timeout):
            logger.warning(
                "Pendo Web UI did not stop within %.1fs",
                normalized_timeout + min(1.0, normalized_timeout),
            )
            return False

        _reset_state()
        _last_error = None
        logger.info("Pendo Web UI stopped")
        return True


def is_managed_running() -> bool:
    """Check whether this process owns a running web server thread."""
    with _STATE_LOCK:
        return _thread is not None and _thread.is_alive()


def is_reachable(timeout: float = 0.3) -> bool:
    """Check whether a web service is reachable at the configured URL."""
    normalized_timeout = _validated_timeout(timeout, field_name="timeout")
    try:
        with urlopen(get_url(), timeout=normalized_timeout) as response:
            return 200 <= int(response.status) < 500
    except (OSError, URLError, TimeoutError):
        return False


def is_running() -> bool:
    """Check if the web UI is running, including an externally started server."""
    return is_managed_running() or is_reachable()


def get_last_error() -> str | None:
    """Return the most recent startup failure for user-facing diagnostics."""
    with _STATE_LOCK:
        return _last_error


def get_url() -> str:
    """Get the web UI URL."""
    runtime = PendoConfig.runtime()
    host = runtime.web_host
    url_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return f"http://{url_host}:{runtime.web_port}"
