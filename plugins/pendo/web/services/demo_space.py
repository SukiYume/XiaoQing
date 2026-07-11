"""Temporary demo-space helpers for public Pendo Web visitors."""

from __future__ import annotations

import json
import threading
import uuid
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ...config import PendoConfig
from ...services.db import Database
from ..auth import AuthError
from .bundle_import import inspect_bundle_bytes

_DEMO_PREFIX = "demo_web_"
_DEMO_TEMPLATE_PATH = Path(__file__).resolve().parent / "assets" / "demo_bundle.pendo.zip"
_DEMO_TEMPLATE_ANCHOR = datetime(2026, 4, 8, 9, 0, 0)
_DATE_FIELDS = {"ledger_date", "diary_date", "plan_date"}
_DATETIME_FIELDS = {
    "created_at",
    "updated_at",
    "start_time",
    "end_time",
    "deadline_at",
    "completed_at",
    "cancelled_at",
    "last_viewed",
    "deleted_at",
}
_REFERENCE_LIST_FIELDS = {"related_items"}
_DEMO_CREATE_LOCK = threading.Lock()
_DEMO_REQUESTS: dict[str, deque[datetime]] = {}


class DemoCapacityError(RuntimeError):
    """Raised when anonymous demo admission would exceed a safe quota."""


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


def _day_text(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d")


def _is_demo_owner(owner_id: str) -> bool:
    return str(owner_id or "").startswith(_DEMO_PREFIX)


def _coerce_expiry(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _demo_settings(expires_at: datetime) -> dict[str, Any]:
    return {
        "reminder_enabled": False,
        "daily_briefing_enabled": False,
        "privacy_mode": True,
        "demo_mode": True,
        "demo_expires_at": _iso(expires_at),
        "demo_seed_version": 2,
    }


def purge_demo_owner(db: Database, owner_id: str) -> None:
    """Hard-delete all data for a temporary demo owner."""
    if not _is_demo_owner(owner_id):
        return

    conn = db.get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM items WHERE owner_id = ?", (owner_id,))
    item_ids = [str(row[0]) for row in cursor.fetchall()]

    with conn:
        if item_ids:
            placeholders = ",".join("?" for _ in item_ids)
            cursor.execute(
                f"DELETE FROM reminder_logs WHERE item_id IN ({placeholders})",
                item_ids,
            )
            cursor.execute(
                f"DELETE FROM items_fts WHERE id IN ({placeholders})",
                item_ids,
            )
        cursor.execute("DELETE FROM items WHERE owner_id = ?", (owner_id,))
        cursor.execute("DELETE FROM user_settings WHERE user_id = ?", (owner_id,))
        cursor.execute("DELETE FROM operation_logs WHERE user_id = ?", (owner_id,))
        cursor.execute("DELETE FROM transfer_logs WHERE owner_id = ?", (owner_id,))
    db.cache_clear()


def purge_expired_demo_users(db: Database, now: datetime | None = None) -> int:
    """Remove expired temporary demo users and their data."""
    now = now or datetime.now()
    conn = db.get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, settings_json FROM user_settings WHERE user_id LIKE ?", (f"{_DEMO_PREFIX}%",))

    expired_user_ids: list[str] = []
    for row in cursor.fetchall():
        user_id = str(row["user_id"])
        try:
            settings_json = json.loads(row["settings_json"] or "{}")
        except (TypeError, json.JSONDecodeError, ValueError):
            settings_json = {}
        if not settings_json.get("demo_mode"):
            continue
        expires_at = _coerce_expiry(settings_json.get("demo_expires_at"))
        if expires_at is not None and expires_at <= now:
            expired_user_ids.append(user_id)

    for owner_id in expired_user_ids:
        purge_demo_owner(db, owner_id)
    return len(expired_user_ids)


def ensure_demo_access(db: Database, owner_id: str, now: datetime | None = None) -> None:
    """Validate that a temporary demo owner is still active."""
    if not _is_demo_owner(owner_id):
        return

    now = now or datetime.now()
    settings = db.get_user_settings(owner_id)
    custom = settings.get("settings_json") or {}
    if not isinstance(custom, dict) or not custom.get("demo_mode"):
        purge_demo_owner(db, owner_id)
        raise AuthError("Demo session is unavailable")

    expires_at = _coerce_expiry(custom.get("demo_expires_at"))
    if expires_at is None or expires_at <= now:
        purge_demo_owner(db, owner_id)
        raise AuthError("Demo session has expired")


def _shift_date_text(value: Any, delta_days: int) -> Any:
    text = str(value or "").strip()
    if not text:
        return value
    try:
        return (datetime.strptime(text, "%Y-%m-%d") + timedelta(days=delta_days)).strftime("%Y-%m-%d")
    except ValueError:
        return value


def _shift_datetime_text(value: Any, delta_days: int) -> Any:
    text = str(value or "").strip()
    if not text:
        return value
    try:
        return (datetime.fromisoformat(text) + timedelta(days=delta_days)).replace(microsecond=0).isoformat()
    except ValueError:
        return value


def _load_demo_template_records() -> list[dict[str, Any]]:
    if not _DEMO_TEMPLATE_PATH.exists():
        raise RuntimeError(f"Missing demo template bundle: {_DEMO_TEMPLATE_PATH}")
    parsed, records, errors = inspect_bundle_bytes(_DEMO_TEMPLATE_PATH.read_bytes())
    if errors:
        raise RuntimeError(f"Invalid demo template bundle with {len(errors)} errors")
    ordered_types = [summary.get("type") for summary in parsed.file_summaries]
    ordered_records: list[dict[str, Any]] = []
    for item_type in ordered_types:
        ordered_records.extend([record for record in records if record.get("type") == item_type])
    return ordered_records or records


def _transform_demo_record(record: dict[str, Any], id_map: dict[str, str], delta_days: int) -> dict[str, Any]:
    transformed = dict(record)

    source_id = str(record.get("id") or "")
    if source_id and source_id in id_map:
        transformed["id"] = id_map[source_id]

    for field in _DATE_FIELDS:
        if field in transformed:
            transformed[field] = _shift_date_text(transformed.get(field), delta_days)

    for field in _DATETIME_FIELDS:
        if field in transformed:
            transformed[field] = _shift_datetime_text(transformed.get(field), delta_days)

    remind_times = transformed.get("remind_times")
    if isinstance(remind_times, list):
        transformed["remind_times"] = [_shift_datetime_text(value, delta_days) for value in remind_times]

    for field in _REFERENCE_LIST_FIELDS:
        values = transformed.get(field)
        if isinstance(values, list):
            transformed[field] = [id_map.get(value, value) for value in values]

    return transformed


def _seed_demo_items(db: Database, owner_id: str, now: datetime) -> None:
    records = _load_demo_template_records()
    delta_days = (now.date() - _DEMO_TEMPLATE_ANCHOR.date()).days
    id_map = {
        str(record["id"]): f"{owner_id}_{record['id']}"
        for record in records
        if record.get("id")
    }
    operations = [
        ("insert", _transform_demo_record(record, id_map, delta_days))
        for record in records
    ]
    db.batch_insert_or_update(operations, owner_id)


def create_demo_session(
    db: Database,
    now: datetime | None = None,
    *,
    client_key: str = "unknown",
) -> dict[str, Any]:
    """Create an isolated temporary demo user and seed its data space."""
    now = now or datetime.now()
    with _DEMO_CREATE_LOCK:
        purge_expired_demo_users(db, now=now)
        request_times = _DEMO_REQUESTS.setdefault(str(client_key or "unknown"), deque())
        cutoff = now - timedelta(hours=1)
        while request_times and request_times[0] <= cutoff:
            request_times.popleft()
        if len(request_times) >= PendoConfig.WEB_DEMO_REQUESTS_PER_HOUR:
            raise DemoCapacityError("Demo creation rate limit exceeded; please try again later")
        conn = db.get_connection()
        active = conn.execute(
            "SELECT COUNT(*) FROM user_settings WHERE user_id LIKE ?",
            (f"{_DEMO_PREFIX}%",),
        ).fetchone()[0]
        if active >= PendoConfig.WEB_DEMO_MAX_ACTIVE_SESSIONS:
            raise DemoCapacityError("Demo capacity is currently full; please try again later")

        owner_id = f"{_DEMO_PREFIX}{uuid.uuid4().hex[:12]}"
        expires_at = now + timedelta(hours=PendoConfig.WEB_DEMO_EXPIRE_HOURS)
        db.update_user_settings(owner_id, {
            "timezone": "Asia/Shanghai",
            "default_category": "未分类",
            "settings_json": _demo_settings(expires_at),
        })
        try:
            _seed_demo_items(db, owner_id, now)
        except Exception:
            purge_demo_owner(db, owner_id)
            raise
        request_times.append(now)

    return {
        "owner_id": owner_id,
        "expires_at": _iso(expires_at),
        "demo": True,
    }
