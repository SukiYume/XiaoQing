"""FastAPI dependency injection for Pendo Web UI."""
from fastapi import Header, HTTPException, Request

from ..services.db import Database
from ..utils.db_ops import set_database_singleton
from .auth import verify_token, AuthError

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
    """Extract owner_id from Bearer token.

    Returns owner_id string.
    Raises 401 if token is missing, invalid, or expired.
    """
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing authorization header")

    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(status_code=401, detail="Invalid authorization header")

    try:
        payload = verify_token(token.strip())
        if payload.get("kind") == "widget":
            path = request.url.path if request is not None else ""
            method = request.method if request is not None else ""
            if method != "GET" or not path.startswith("/api/widget/"):
                raise HTTPException(
                    status_code=403,
                    detail="Widget token is limited to /api/widget/* read-only requests",
                )
        owner_id = payload["owner_id"]
        if payload.get("demo"):
            from .services.demo_space import ensure_demo_access

            ensure_demo_access(get_db(), owner_id)
        return owner_id
    except AuthError as e:
        raise HTTPException(status_code=401, detail=e.message)
