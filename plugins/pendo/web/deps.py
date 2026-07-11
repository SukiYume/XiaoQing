"""FastAPI dependency injection for Pendo Web UI."""
import secrets

from fastapi import Header, HTTPException, Request

from ..services.db import Database
from ..utils.db_ops import set_database_singleton
from .auth import AuthError, WebSession, get_web_session, verify_token

SESSION_COOKIE_NAME = "pendo_web_session"
CSRF_HEADER_NAME = "X-CSRF-Token"
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

# Module-level reference, set by server.py on startup
_db_instance: Database | None = None


def set_db(db: Database) -> None:
    """Set the database instance (called on server start)."""
    global _db_instance
    _db_instance = db
    set_database_singleton(db)


def get_db() -> Database:
    """Get the shared Database instance."""
    if _db_instance is None:
        raise HTTPException(status_code=503, detail="Database not initialized")
    return _db_instance


def get_current_user(
    request: Request,
    authorization: str | None = Header(default=None),
) -> str:
    try:
        if authorization:
            scheme, _, token = authorization.partition(" ")
            if scheme.lower() != "bearer" or not token.strip():
                raise HTTPException(status_code=401, detail="Invalid authorization header")
            payload = verify_token(token.strip())
            if payload.get("kind") != "widget":
                raise HTTPException(
                    status_code=401,
                    detail="Browser bearer tokens are no longer accepted; use a web login code",
                )
            path = request.url.path if request is not None else ""
            method = request.method if request is not None else ""
            if method != "GET" or not path.startswith("/api/widget/"):
                raise HTTPException(
                    status_code=403,
                    detail="Widget token is limited to /api/widget/* read-only requests",
                )
            return payload["owner_id"]

        session = get_current_session(request)
        if request.method not in _SAFE_METHODS:
            csrf = request.headers.get(CSRF_HEADER_NAME, "")
            if not csrf or not secrets.compare_digest(csrf, session.csrf_token):
                raise HTTPException(status_code=403, detail="Missing or invalid CSRF token")
        owner_id = session.owner_id
        if session.demo:
            from .services.demo_space import ensure_demo_access

            ensure_demo_access(get_db(), owner_id)
        return owner_id
    except AuthError as e:
        raise HTTPException(status_code=401, detail=e.message) from e


def get_current_session(request: Request) -> WebSession:
    """Return the authenticated browser session without accepting a bearer token."""

    try:
        session = get_web_session(request.cookies.get(SESSION_COOKIE_NAME))
        if session.demo:
            from .services.demo_space import ensure_demo_access

            ensure_demo_access(get_db(), session.owner_id)
        return session
    except AuthError as e:
        raise HTTPException(status_code=401, detail=e.message) from e
