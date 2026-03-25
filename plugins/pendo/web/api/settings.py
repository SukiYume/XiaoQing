"""User settings endpoints."""
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from typing import Optional

from ...services.db import Database
from ..deps import get_db, get_current_user

router = APIRouter()


class SettingsUpdate(BaseModel):
    timezone: Optional[str] = None
    quiet_hours_start: Optional[str] = None
    quiet_hours_end: Optional[str] = None
    daily_report_time: Optional[str] = None
    diary_remind_time: Optional[str] = None
    default_category: Optional[str] = None
    settings_json: Optional[dict] = None


@router.get("/settings")
def get_settings(
    owner_id: str = Depends(get_current_user),
    db: Database = Depends(get_db),
):
    """Get user settings."""
    settings = db.get_user_settings(owner_id)
    return {"ok": True, "data": settings, "message": ""}


@router.put("/settings")
def update_settings(
    body: SettingsUpdate,
    owner_id: str = Depends(get_current_user),
    db: Database = Depends(get_db),
):
    """Update user settings."""
    updates = body.model_dump(exclude_none=True)
    if updates:
        db.update_user_settings(owner_id, updates)
    settings = db.get_user_settings(owner_id)
    return {"ok": True, "data": settings, "message": "设置已更新"}
