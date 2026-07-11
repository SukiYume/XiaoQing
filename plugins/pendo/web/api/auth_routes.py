"""Short-lived login-code exchange and browser-session routes."""
import time

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from ...config import PendoConfig
from ...services.db import Database
from ..auth import (
    AuthError,
    WebSession,
    consume_login_code,
    create_web_session,
    list_web_sessions,
    revoke_web_session,
    revoke_web_session_device,
)
from ..deps import SESSION_COOKIE_NAME, get_current_session, get_current_user, get_db
from ..services.demo_space import DemoCapacityError, create_demo_session

router = APIRouter()


class LoginExchangeRequest(BaseModel):
    code: str


def _session_payload(session: WebSession) -> dict[str, object]:
    return {
        "owner_id": session.owner_id,
        "expires_at": int(session.expires_at),
        "csrf_token": session.csrf_token,
        "demo": session.demo,
    }


def _device_payload(session: WebSession, current_session: WebSession) -> dict[str, object]:
    return {
        "device_id": session.device_id,
        "created_at": int(session.created_at),
        "expires_at": int(session.expires_at),
        "current": session.session_id == current_session.session_id,
    }


def _set_session_cookie(response: Response, session: WebSession) -> None:
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=session.session_id,
        max_age=max(1, int(session.expires_at - time.time())),
        httponly=True,
        secure=PendoConfig.WEB_SESSION_COOKIE_SECURE,
        samesite="strict",
        path="/",
    )


@router.post("/auth/exchange")
def exchange_login_code(body: LoginExchangeRequest, response: Response):
    """Exchange one private, short-lived code for a server-side session cookie."""
    try:
        owner_id = consume_login_code(body.code)
    except AuthError as exc:
        raise HTTPException(status_code=401, detail=exc.message) from exc
    session = create_web_session(
        owner_id,
        expires_seconds=PendoConfig.WEB_SESSION_EXPIRE_SECONDS,
    )
    _set_session_cookie(response, session)
    return {"ok": True, "data": _session_payload(session), "message": ""}


@router.post("/auth/demo")
def create_demo_auth(
    request: Request,
    response: Response,
    db: Database = Depends(get_db),
):
    """Create a temporary public demo session."""
    if not PendoConfig.WEB_DEMO_ENABLED:
        raise HTTPException(status_code=404, detail="Demo mode is disabled")
    client = request.client.host if request.client else "unknown"
    try:
        demo = create_demo_session(db=db, client_key=client)
    except DemoCapacityError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    session = create_web_session(
        demo["owner_id"],
        expires_seconds=PendoConfig.WEB_SESSION_EXPIRE_SECONDS,
        demo=True,
    )
    _set_session_cookie(response, session)
    return {"ok": True, "data": _session_payload(session), "message": ""}


@router.get("/auth/session")
def get_auth_session(session: WebSession = Depends(get_current_session)):
    """Return non-secret browser-session metadata for application bootstrap."""
    return {"ok": True, "data": _session_payload(session), "message": ""}


@router.get("/auth/sessions")
def get_auth_sessions(session: WebSession = Depends(get_current_session)):
    """List the current owner's browser sessions without exposing bearer values."""
    sessions = list_web_sessions(session.owner_id)
    return {
        "ok": True,
        "data": {"sessions": [_device_payload(item, session) for item in sessions]},
        "message": "",
    }


@router.delete("/auth/sessions/{device_id}")
def revoke_auth_session(
    device_id: str,
    session: WebSession = Depends(get_current_session),
    owner_id: str = Depends(get_current_user),
):
    """Revoke a selected session after cookie authentication and CSRF validation."""
    if not revoke_web_session_device(owner_id, device_id):
        raise HTTPException(status_code=404, detail="Session device was not found")
    return {"ok": True, "data": {"current": device_id == session.device_id}, "message": ""}


@router.post("/auth/logout")
def logout(
    response: Response,
    session: WebSession = Depends(get_current_session),
    owner_id: str = Depends(get_current_user),
):
    """Revoke the current server-side session and clear its cookie."""
    revoke_web_session(session.session_id)
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        httponly=True,
        secure=PendoConfig.WEB_SESSION_COOKIE_SECURE,
        samesite="strict",
        path="/",
    )
    return {"ok": True, "data": {}, "message": ""}
