"""Auth verification endpoint."""
from fastapi import APIRouter, Depends, Header

from ..auth import verify_token
from ..deps import get_current_user


router = APIRouter()


@router.post("/auth/verify")
def verify_auth(
    owner_id: str = Depends(get_current_user),
    authorization: str | None = Header(default=None),
):
    """Verify token validity and return user info."""
    token = (authorization or "").partition(" ")[2].strip()
    payload = verify_token(token)
    return {
        "ok": True,
        "data": {
            "owner_id": owner_id,
            "expires_at": payload.get("exp"),
            "issued_at": payload.get("iat"),
        },
        "message": "",
    }
