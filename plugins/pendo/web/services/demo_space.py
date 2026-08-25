"""为 Pendo Web 公共访客创建、校验并回收隔离的临时演示空间。"""

from __future__ import annotations

import json
import threading
import uuid
from collections import deque
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Final

from ...config import PendoConfig
from ...services.db import Database
from ...utils.identifiers import new_internal_id
from ..auth import AuthError
from .bundle_import import inspect_bundle_bytes

_DEMO_PREFIX: Final = "demo_web_"
_DEMO_TEMPLATE_PATH: Final = Path(__file__).resolve().parent / "assets" / "demo_bundle.pendo.zip"
_DEMO_TEMPLATE_ANCHOR: Final = date(2026, 4, 8)
_DEMO_SEED_VERSION: Final = 2
_DEMO_ITEM_TYPES: Final = ("event", "task", "ledger", "note", "diary")
_MAX_CLIENT_KEY_CHARS: Final = 128
_DATE_FIELDS: Final = ("ledger_date", "diary_date", "plan_date")
_DATETIME_FIELDS: Final = (
    "created_at",
    "updated_at",
    "start_time",
    "end_time",
    "deadline_at",
    "completed_at",
    "cancelled_at",
    "last_viewed",
    "deleted_at",
)
_DEMO_CREATE_LOCK = threading.Lock()
_DEMO_REQUESTS: dict[str, deque[datetime]] = {}


class DemoCapacityError(RuntimeError):
    """匿名演示请求超过速率或活动会话容量时抛出。"""


def _iso(dt: datetime) -> str:
    """生成不含微秒、可稳定写入设置或响应的 ISO 时间。"""

    return dt.replace(microsecond=0).isoformat()


def _is_demo_owner(owner_id: str) -> bool:
    """判断所有者是否属于演示空间保留命名域。"""

    return str(owner_id or "").startswith(_DEMO_PREFIX)


def _coerce_expiry(value: object) -> datetime | None:
    """严格解析演示过期时间；非字符串或坏格式均视为无效。"""

    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except (OverflowError, ValueError):
        return None


def _is_expired(expires_at: datetime | None, now: datetime) -> bool:
    """兼容历史本地时间与带时区时间，并对异常时间失败关闭。"""

    if expires_at is None:
        return True
    try:
        expires_offset = expires_at.utcoffset()
        now_offset = now.utcoffset()
        if (expires_offset is None) == (now_offset is None):
            return expires_at <= now
        return expires_at.astimezone(timezone.utc) <= now.astimezone(timezone.utc)
    except (OSError, OverflowError, TypeError, ValueError):
        return True


def _demo_settings(expires_at: datetime) -> dict[str, Any]:
    """构造最小且关闭主动通知能力的演示用户设置。"""

    return {
        "reminder_enabled": False,
        "daily_briefing_enabled": False,
        "demo_mode": True,
        "demo_expires_at": _iso(expires_at),
        "demo_seed_version": _DEMO_SEED_VERSION,
    }


def purge_demo_owner(db: Database, owner_id: str) -> None:
    """只对演示命名域执行硬删除，并清空该所有者的全部持久数据。"""

    if not _is_demo_owner(owner_id):
        return

    conn = db.get_connection()
    cursor = conn.cursor()
    with conn:
        # 先按所有者子查询清除无外键约束的条目附属数据，避免动态占位符上限。
        cursor.execute(
            "DELETE FROM reminder_logs WHERE item_id IN (SELECT id FROM items WHERE owner_id = ?)",
            (owner_id,),
        )
        cursor.execute(
            "DELETE FROM items_fts WHERE id IN (SELECT id FROM items WHERE owner_id = ?)",
            (owner_id,),
        )
        cursor.execute("DELETE FROM items WHERE owner_id = ?", (owner_id,))
        cursor.execute("DELETE FROM event_collections WHERE owner_id = ?", (owner_id,))
        cursor.execute("DELETE FROM scheduled_delivery_outbox WHERE owner_id = ?", (owner_id,))
        cursor.execute("DELETE FROM user_settings WHERE user_id = ?", (owner_id,))
        cursor.execute("DELETE FROM operation_logs WHERE user_id = ?", (owner_id,))
        cursor.execute("DELETE FROM transfer_logs WHERE owner_id = ?", (owner_id,))
        cursor.execute("DELETE FROM imported_bundles WHERE owner_id = ?", (owner_id,))
        cursor.execute("DELETE FROM login_code_registry WHERE owner_id = ?", (owner_id,))
        cursor.execute("DELETE FROM web_session_registry WHERE owner_id = ?", (owner_id,))
        cursor.execute("DELETE FROM widget_token_registry WHERE owner_id = ?", (owner_id,))
    db.cache_clear()


def purge_expired_demo_users(db: Database, now: datetime | None = None) -> int:
    """回收过期或设置损坏的演示用户，避免其永久占用容量。"""

    now = now or datetime.now()
    conn = db.get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT user_id, settings_json FROM user_settings WHERE user_id LIKE ?",
        (f"{_DEMO_PREFIX}%",),
    )

    expired_user_ids: list[str] = []
    for row in cursor.fetchall():
        user_id = str(row["user_id"])
        try:
            loaded_settings: object = json.loads(row["settings_json"] or "{}")
        except (TypeError, json.JSONDecodeError, ValueError):
            loaded_settings = None
        settings_json = loaded_settings if isinstance(loaded_settings, dict) else None
        if settings_json is None or settings_json.get("demo_mode") is not True:
            expired_user_ids.append(user_id)
            continue
        expires_at = _coerce_expiry(settings_json.get("demo_expires_at"))
        if _is_expired(expires_at, now):
            expired_user_ids.append(user_id)

    for owner_id in expired_user_ids:
        purge_demo_owner(db, owner_id)
    return len(expired_user_ids)


def ensure_demo_access(db: Database, owner_id: str, now: datetime | None = None) -> None:
    """确认演示所有者仍有效；失效会话立即回收其数据并拒绝访问。"""

    if not _is_demo_owner(owner_id):
        return

    now = now or datetime.now()
    settings = db.get_user_settings(owner_id)
    custom = settings.get("settings_json") or {}
    if not isinstance(custom, dict) or custom.get("demo_mode") is not True:
        purge_demo_owner(db, owner_id)
        raise AuthError("Demo session is unavailable")

    expires_at = _coerce_expiry(custom.get("demo_expires_at"))
    if _is_expired(expires_at, now):
        purge_demo_owner(db, owner_id)
        raise AuthError("Demo session has expired")


def _shift_date_text(value: object, delta_days: int) -> object:
    """把模板纯日期平移到当前演示窗口，坏值保持原样交由存储层校验。"""

    text = str(value or "").strip()
    if not text:
        return value
    try:
        return (date.fromisoformat(text) + timedelta(days=delta_days)).isoformat()
    except (OverflowError, ValueError):
        return value


def _shift_datetime_text(value: object, delta_days: int) -> object:
    """平移模板 ISO 时间并去掉微秒，无法解析或越界时保留原值。"""

    text = str(value or "").strip()
    if not text:
        return value
    try:
        return (
            (datetime.fromisoformat(text) + timedelta(days=delta_days))
            .replace(microsecond=0)
            .isoformat()
        )
    except (OverflowError, ValueError):
        return value


def _load_demo_template_records() -> list[dict[str, Any]]:
    """读取并验证内置演示包，按清单类型顺序返回全部条目。"""

    if not _DEMO_TEMPLATE_PATH.exists():
        raise RuntimeError(f"Missing demo template bundle: {_DEMO_TEMPLATE_PATH}")
    parsed, records, errors = inspect_bundle_bytes(_DEMO_TEMPLATE_PATH.read_bytes())
    if errors:
        raise RuntimeError(f"Invalid demo template bundle with {len(errors)} errors")
    if parsed.event_collections:
        raise RuntimeError("Demo template bundle must not contain event collections")

    ordered_types = tuple(str(summary.get("type") or "") for summary in parsed.file_summaries)
    if len(ordered_types) != len(_DEMO_ITEM_TYPES) or set(ordered_types) != set(_DEMO_ITEM_TYPES):
        raise RuntimeError("Demo template bundle must contain each demo item type once")

    records_by_type: dict[str, list[dict[str, Any]]] = {
        item_type: [] for item_type in _DEMO_ITEM_TYPES
    }
    for record in records:
        item_type = str(record.get("type") or "")
        if item_type not in records_by_type:
            raise RuntimeError(f"Unsupported demo template item type: {item_type}")
        records_by_type[item_type].append(record)
    if any(not records_by_type[item_type] for item_type in _DEMO_ITEM_TYPES):
        raise RuntimeError("Demo template bundle contains an empty item type")

    return [record for item_type in ordered_types for record in records_by_type[item_type]]


def _transform_demo_record(
    record: dict[str, Any], id_map: dict[str, str], delta_days: int
) -> dict[str, Any]:
    """复制模板条目，同时平移日期并重写条目 ID 与内部引用。"""

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
        transformed["remind_times"] = [
            _shift_datetime_text(value, delta_days) for value in remind_times
        ]

    related_items = transformed.get("related_items")
    if isinstance(related_items, list):
        transformed["related_items"] = [
            id_map.get(value, value) if isinstance(value, str) else value for value in related_items
        ]

    return transformed


def _seed_demo_items(db: Database, owner_id: str, now: datetime) -> None:
    """为一个新演示所有者批量写入经平移和隔离后的模板条目。"""

    records = _load_demo_template_records()
    delta_days = (now.date() - _DEMO_TEMPLATE_ANCHOR).days
    id_map = {str(record["id"]): new_internal_id() for record in records if record.get("id")}
    operations = [
        ("insert", _transform_demo_record(record, id_map, delta_days)) for record in records
    ]
    db.batch_insert_or_update(operations, owner_id)


def _recent_demo_requests(
    client_key: str,
    now: datetime,
) -> tuple[str, deque[datetime]]:
    """清理所有过期限流桶，并返回规范化客户端的一小时窗口。"""

    cutoff = now - timedelta(hours=1)
    for key, request_times in tuple(_DEMO_REQUESTS.items()):
        while request_times and _is_expired(request_times[0], cutoff):
            request_times.popleft()
        if not request_times:
            del _DEMO_REQUESTS[key]

    normalized_key = str(client_key or "unknown").strip()[:_MAX_CLIENT_KEY_CHARS]
    normalized_key = normalized_key or "unknown"
    return normalized_key, _DEMO_REQUESTS.get(normalized_key, deque())


def create_demo_session(
    db: Database,
    now: datetime | None = None,
    *,
    client_key: str = "unknown",
) -> dict[str, Any]:
    """在容量和单客户端速率限制内创建隔离、带样例数据的演示空间。"""

    now = now or datetime.now()
    with _DEMO_CREATE_LOCK:
        purge_expired_demo_users(db, now=now)
        normalized_client, request_times = _recent_demo_requests(client_key, now)
        if len(request_times) >= PendoConfig.WEB_DEMO_REQUESTS_PER_HOUR:
            raise DemoCapacityError("Demo creation rate limit exceeded; please try again later")
        conn = db.get_connection()
        active = conn.execute(
            "SELECT COUNT(*) FROM user_settings WHERE user_id LIKE ?",
            (f"{_DEMO_PREFIX}%",),
        ).fetchone()[0]
        if active >= PendoConfig.WEB_DEMO_MAX_ACTIVE_SESSIONS:
            raise DemoCapacityError("Demo capacity is currently full; please try again later")

        owner_id = f"{_DEMO_PREFIX}{uuid.uuid4().hex}"
        expires_at = now + timedelta(hours=PendoConfig.WEB_DEMO_EXPIRE_HOURS)
        db.update_user_settings(
            owner_id,
            {
                "timezone": PendoConfig.DEFAULT_TIMEZONE,
                "default_category": "未分类",
                "settings_json": _demo_settings(expires_at),
            },
        )
        try:
            _seed_demo_items(db, owner_id, now)
        except Exception:
            purge_demo_owner(db, owner_id)
            raise
        request_times.append(now)
        _DEMO_REQUESTS[normalized_client] = request_times

    return {
        "owner_id": owner_id,
        "expires_at": _iso(expires_at),
        "demo": True,
    }
