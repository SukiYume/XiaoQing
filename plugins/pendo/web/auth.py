"""JWT token generation and verification for Pendo Web UI."""
import os
import secrets
import time
from pathlib import Path

import jwt

_ALGORITHM = "HS256"
_SECRET_FILE = Path(__file__).resolve().parents[1] / "data" / "web_token_secret.txt"
_SECRET_CACHE: str | None = None


class AuthError(Exception):
    """Authentication error."""
    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


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
    _SECRET_FILE.write_text(generated_secret, encoding="utf-8")
    _SECRET_CACHE = generated_secret
    return _SECRET_CACHE


def generate_token(owner_id: str, expires_hours: int = 24) -> str:
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
    return jwt.encode(payload, _get_secret_key(), algorithm=_ALGORITHM)


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
    except jwt.ExpiredSignatureError:
        raise AuthError("Token has expired")
    except jwt.InvalidTokenError as e:
        raise AuthError(f"Invalid token: {e}")
