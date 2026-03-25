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
