"""Auth verification endpoint."""
from fastapi import APIRouter, Depends, Header, HTTPException

from ...config import PendoConfig
from ...services.db import Database
from ..auth import verify_token
from ..deps import get_current_user, get_db
from ..services.demo_space import create_demo_session


router = APIRouter()


@router.post("/auth/demo")
def create_demo_auth(
    db: Database = Depends(get_db),
):
    """Create a temporary public demo session."""
    if not PendoConfig.WEB_DEMO_ENABLED:
        raise HTTPException(status_code=404, detail="Demo mode is disabled")
    session = create_demo_session(db=db)
    return {"ok": True, "data": session, "message": ""}


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
