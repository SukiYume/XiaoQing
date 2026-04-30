"""User settings endpoints."""
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ...services.db import Database
from ...utils.settings_utils import normalize_settings_json
from ...utils.validators import validate_category
from ..deps import get_current_user, get_db

router = APIRouter()


class SettingsUpdate(BaseModel):
    timezone: str | None = None
    quiet_hours_start: str | None = None
    quiet_hours_end: str | None = None
    daily_report_time: str | None = None
    diary_remind_time: str | None = None
    default_category: str | None = None
    settings_json: dict | None = None


def _normalize_time_text(value: str, field_name: str) -> str:
    try:
        return datetime.strptime(value, "%H:%M").strftime("%H:%M")
    except ValueError as exc:
        raise ValueError(f"Invalid {field_name}, expected HH:MM") from exc


def _normalize_settings_payload(updates: dict) -> dict:
    normalized = dict(updates)

    timezone = normalized.get("timezone")
    if timezone is not None:
        try:
            ZoneInfo(timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Invalid timezone: {timezone}") from exc

    for field in ("quiet_hours_start", "quiet_hours_end", "daily_report_time", "diary_remind_time"):
        if normalized.get(field) is not None:
            normalized[field] = _normalize_time_text(str(normalized[field]), field)

    if normalized.get("default_category") is not None:
        category = str(normalized["default_category"]).strip()
        normalized["default_category"] = validate_category(category or "未分类")

    if normalized.get("settings_json") is not None:
        if not isinstance(normalized["settings_json"], dict):
            raise ValueError("settings_json must be an object")
        normalized["settings_json"] = normalize_settings_json(normalized["settings_json"], partial=True)

    return normalized


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
        try:
            updates = _normalize_settings_payload(updates)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if not db.update_user_settings(owner_id, updates):
            raise HTTPException(status_code=500, detail="Failed to update settings")
    settings = db.get_user_settings(owner_id)
    return {"ok": True, "data": settings, "message": "设置已更新"}
