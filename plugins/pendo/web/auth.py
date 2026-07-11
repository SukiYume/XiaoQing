"""JWT token generation and verification for Pendo Web UI."""
import os
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import jwt

from core.plugin_base import atomic_write_text

_ALGORITHM = "HS256"
_SECRET_FILE = Path(__file__).resolve().parents[1] / "data" / "web_token_secret.txt"
_SECRET_CACHE: str | None = None
_AUTH_LOCK = threading.Lock()
_LOGIN_CODES: dict[str, "LoginCode"] = {}
_SESSIONS: dict[str, "WebSession"] = {}


class AuthError(Exception):
    """Authentication error."""
    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class LoginCode:
    owner_id: str
    expires_at: float


@dataclass(frozen=True)
class WebSession:
    session_id: str
    device_id: str
    owner_id: str
    csrf_token: str
    created_at: float
    expires_at: float
    demo: bool = False


def _prune_expired(now: float) -> None:
    for code, grant in list(_LOGIN_CODES.items()):
        if grant.expires_at <= now:
            _LOGIN_CODES.pop(code, None)
    for session_id, session in list(_SESSIONS.items()):
        if session.expires_at <= now:
            _SESSIONS.pop(session_id, None)


def issue_login_code(owner_id: str, expires_seconds: int = 300) -> str:
    """Create a short-lived, single-use login exchange code."""

    if not isinstance(owner_id, str) or not owner_id.strip():
        raise ValueError("owner_id is required")
    lifetime = max(1, int(expires_seconds))
    code = secrets.token_urlsafe(32)
    now = time.time()
    with _AUTH_LOCK:
        _prune_expired(now)
        _LOGIN_CODES[code] = LoginCode(owner_id=owner_id.strip(), expires_at=now + lifetime)
    return code


def consume_login_code(code: str) -> str:
    """Atomically consume a login code and return its bound owner ID."""

    if not isinstance(code, str) or not code.strip():
        raise AuthError("Missing login code")
    now = time.time()
    with _AUTH_LOCK:
        _prune_expired(now)
        grant = _LOGIN_CODES.pop(code.strip(), None)
    if grant is None:
        raise AuthError("Login code is invalid, expired, or already used")
    return grant.owner_id


def create_web_session(
    owner_id: str,
    *,
    expires_seconds: int = 8 * 60 * 60,
    demo: bool = False,
) -> WebSession:
    """Create a revocable server-side browser session and CSRF token."""

    lifetime = max(1, int(expires_seconds))
    now = time.time()
    session = WebSession(
        session_id=secrets.token_urlsafe(32),
        device_id=secrets.token_urlsafe(12),
        owner_id=owner_id,
        csrf_token=secrets.token_urlsafe(32),
        created_at=now,
        expires_at=now + lifetime,
        demo=demo,
    )
    with _AUTH_LOCK:
        _prune_expired(now)
        _SESSIONS[session.session_id] = session
    return session


def get_web_session(session_id: str | None) -> WebSession:
    if not session_id:
        raise AuthError("Missing web session")
    now = time.time()
    with _AUTH_LOCK:
        _prune_expired(now)
        session = _SESSIONS.get(session_id)
    if session is None:
        raise AuthError("Web session is invalid or expired")
    return session


def revoke_web_session(session_id: str | None) -> None:
    if not session_id:
        return
    with _AUTH_LOCK:
        _SESSIONS.pop(session_id, None)


def list_web_sessions(owner_id: str) -> list[WebSession]:
    """List non-secret session records so users can review their devices."""

    now = time.time()
    with _AUTH_LOCK:
        _prune_expired(now)
        return sorted(
            (session for session in _SESSIONS.values() if session.owner_id == owner_id),
            key=lambda session: session.created_at,
            reverse=True,
        )


def revoke_web_session_device(owner_id: str, device_id: str) -> bool:
    """Revoke an owner's session by its non-secret device identifier."""

    with _AUTH_LOCK:
        for session_id, session in list(_SESSIONS.items()):
            if session.owner_id == owner_id and session.device_id == device_id:
                _SESSIONS.pop(session_id, None)
                return True
    return False


def _get_secret_key() -> str:
    """Load a stable secret key for signing web tokens.

    Priority:
    1. `PENDO_WEB_TOKEN_SECRET` environment variable
    2. persisted secret file under `plugins/pendo/data`
    """
    global _SECRET_CACHE
    if _SECRET_CACHE:
        return _SECRET_CACHE

    env_secret = os.getenv("PENDO_WEB_TOKEN_SECRET", "").strip()
    if env_secret:
        _SECRET_CACHE = env_secret
        return _SECRET_CACHE

    _SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
    if _SECRET_FILE.exists():
        saved_secret = _SECRET_FILE.read_text(encoding="utf-8").strip()
        if saved_secret:
            _SECRET_CACHE = saved_secret
            return _SECRET_CACHE

    generated_secret = secrets.token_hex(32)
    atomic_write_text(_SECRET_FILE, generated_secret)
    _SECRET_CACHE = generated_secret
    return _SECRET_CACHE


def generate_token(owner_id: str, expires_hours: int = 24, extra_claims: dict | None = None) -> str:
    """Generate a JWT token for the given owner_id."""
    now = int(time.time())
    payload = {
        "owner_id": owner_id,
        "sub": owner_id,
        "typ": "pendo-web",
        "iss": "pendo-web",
        "exp": now + expires_hours * 3600,
        "iat": now,
    }
    if extra_claims:
        payload.update(extra_claims)
    return jwt.encode(payload, _get_secret_key(), algorithm=_ALGORITHM)


def generate_widget_token(owner_id: str, expires_hours: int = 24 * 180) -> str:
    """Generate a long-lived read-only token for widget consumption."""
    return generate_token(
        owner_id,
        expires_hours=expires_hours,
        extra_claims={
            "kind": "widget",
            "scope": "widget:read",
        },
    )


def verify_token(token: str) -> dict:
    """Verify and decode a JWT token. Returns payload dict.

    Raises AuthError if token is invalid or expired.
    """
    try:
        payload = jwt.decode(
            token,
            _get_secret_key(),
            algorithms=[_ALGORITHM],
            issuer="pendo-web",
            options={"require": ["exp", "iat", "owner_id"]},
        )
        owner_id = payload.get("owner_id")
        if not isinstance(owner_id, str) or not owner_id.strip():
            raise AuthError("Token missing owner_id")
        return payload
    except jwt.ExpiredSignatureError as exc:
        raise AuthError("Token has expired") from exc
    except jwt.InvalidTokenError as e:
        raise AuthError(f"Invalid token: {e}") from e
