"""Auth verification endpoint."""
from fastapi import APIRouter, Depends

from ..deps import get_current_user

router = APIRouter()


@router.post("/auth/verify")
def verify_auth(owner_id: str = Depends(get_current_user)):
    """Verify token validity and return user info."""
    return {"ok": True, "data": {"owner_id": owner_id}, "message": ""}
