"""提供 Pendo 用户设置的读取、校验与幂等更新端点。"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict

from ...services.db import Database
from ...utils.settings_utils import normalize_settings_json
from ...utils.validators import validate_category
from ..deps import get_current_user, get_db

router = APIRouter()


class SettingsUpdate(BaseModel):  # type: ignore[misc]
    """允许 Web 端更新的设置字段；未知字段必须显式失败。"""

    model_config = ConfigDict(extra="forbid")

    timezone: str | None                 = None
    quiet_hours_start: str | None        = None
    quiet_hours_end: str | None          = None
    daily_report_time: str | None        = None
    diary_remind_time: str | None        = None
    default_category: str | None         = None
    settings_json: dict[str, Any] | None = None


def _normalize_time_text(value: str, field_name: str) -> str:
    """把用户时间规范为零填充的 24 小时 ``HH:MM``。"""

    try:
        return datetime.strptime(value.strip(), "%H:%M").strftime("%H:%M")
    except ValueError as exc:
        raise ValueError(f"Invalid {field_name}, expected HH:MM") from exc


def _normalize_settings_payload(updates: dict[str, Any]) -> dict[str, Any]:
    """规范一个设置补丁，不改动调用方传入的字典。"""

    normalized = dict(updates)

    timezone = normalized.get("timezone")
    if timezone is not None:
        timezone = str(timezone).strip()
        if not timezone:
            raise ValueError("Invalid timezone")
        try:
            ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("Invalid timezone") from exc
        normalized["timezone"] = timezone

    for field in ("quiet_hours_start", "quiet_hours_end", "daily_report_time", "diary_remind_time"):
        if normalized.get(field) is not None:
            normalized[field] = _normalize_time_text(str(normalized[field]), field)

    if normalized.get("default_category") is not None:
        category                       = str(normalized["default_category"]).strip()
        normalized["default_category"] = validate_category(category or "未分类")

    if normalized.get("settings_json") is not None:
        if not isinstance(normalized["settings_json"], dict):
            raise ValueError("settings_json must be an object")
        normalized["settings_json"] = normalize_settings_json(
            normalized["settings_json"], partial=True
        )

    return normalized


def _settings_patch_changes(
    current: dict[str, Any],
    updates: dict[str, Any],
) -> bool:
    """判断规范化补丁是否会改变标量设置或 JSON 扩展键。"""

    for key, value in updates.items():
        if key != "settings_json":
            if current.get(key) != value:
                return True
            continue
        current_custom = current.get("settings_json")
        current_custom = current_custom if isinstance(current_custom, dict) else {}
        if any(
            current_custom.get(custom_key) != custom_value
            for custom_key, custom_value in value.items()
        ):
            return True
    return False


@router.get("/settings")
def get_settings(
    owner_id: str = Depends(get_current_user),
    db: Database  = Depends(get_db),
) -> dict[str, object]:
    """返回当前所有者的持久化设置或完整默认值。"""

    settings = db.get_user_settings(owner_id)
    return {"ok": True, "data": settings, "message": ""}


@router.put("/settings")
def update_settings(
    body: SettingsUpdate,
    owner_id: str = Depends(get_current_user),
    db: Database  = Depends(get_db),
) -> dict[str, object]:
    """校验设置补丁，仅在实际变化时写库并增加版本。"""

    updates: dict[str, Any] = body.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(status_code=422, detail="No settings fields to update")
    try:
        updates = _normalize_settings_payload(updates)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    current = db.get_user_settings(owner_id)
    if not _settings_patch_changes(current, updates):
        return {"ok": True, "data": current, "message": "无变化"}
    if not db.update_user_settings(owner_id, updates):
        raise HTTPException(status_code=500, detail="Failed to update settings")
    settings = db.get_user_settings(owner_id)
    return {"ok": True, "data": settings, "message": "设置已更新"}
