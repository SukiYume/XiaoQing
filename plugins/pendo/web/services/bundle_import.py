"""Pendo 传输包的来源时区转换与入库前字段规范化。"""

from __future__ import annotations

import io
from datetime import UTC, datetime
from typing import Any, Final, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ...config import PendoConfig
from ...utils.time_utils import normalize_datetime_for_storage
from ...utils.validators import (
    normalize_bool_flag,
    normalize_item_fields,
    normalize_reminder_rules,
    sanitize_text,
    validate_category,
    validate_location,
    validate_tag,
    validate_title,
)
from .transfer_bundle import (
    EVENT_COLLECTION_FILE_NAME,
    TYPE_FILE_NAMES,
    ParsedBundle,
    read_bundle,
)

_ITEM_DATETIME_FIELDS: Final = (
    "created_at",
    "updated_at",
    "deleted_at",
    "start_time",
    "end_time",
    "deadline_at",
    "completed_at",
    "cancelled_at",
    "last_viewed",
    "entry_time",
)
_COLLECTION_DATETIME_FIELDS: Final = (
    "created_at",
    "updated_at",
    "deleted_at",
    "start_time",
    "end_time",
)
_COLLECTION_KINDS: Final        = frozenset({"multi_node", "recurring"})
_COLLECTION_VISIBILITIES: Final = frozenset({"private", "group_scope"})
_DEFAULT_SOURCE_ZONE: Final     = ZoneInfo(PendoConfig.DEFAULT_TIMEZONE)


def _normalization_context(
    source_zone: ZoneInfo | None,
    now: datetime | None,
) -> tuple[ZoneInfo, str]:
    """解析来源时区，并生成一次性的 UTC 秒级导入时间戳。"""

    zone    = source_zone if source_zone is not None else _DEFAULT_SOURCE_ZONE
    current = now if now is not None else datetime.now(UTC)
    try:
        offset = current.utcoffset()
    except (OverflowError, ValueError) as exc:
        raise ValueError("import clock is invalid") from exc
    if offset is None:
        raise ValueError("import clock must be timezone-aware")
    try:
        timestamp = current.astimezone(UTC).isoformat(timespec="seconds")
    except (OverflowError, ValueError) as exc:
        raise ValueError("import clock is outside the supported range") from exc
    return zone, timestamp


def _localize_source_datetime(
    value: object,
    field_name: str,
    source_zone: ZoneInfo,
) -> str | None:
    """把来源时区中的 ISO 时间转换为 UTC。"""

    if value is None:
        return None
    if value == "":
        return ""
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(f"Invalid {field_name}, expected ISO datetime") from exc

    return normalize_datetime_for_storage(parsed, field_name, source_zone)


def _normalize_source_datetimes(
    payload: dict[str, Any],
    fields: tuple[str, ...],
    source_zone: ZoneInfo,
) -> dict[str, Any]:
    """复制载荷，并按确定顺序规范普通时间字段和绝对提醒时间。"""

    normalized = dict(payload)
    for field_name in fields:
        if field_name in normalized:
            normalized[field_name] = _localize_source_datetime(
                normalized[field_name],
                field_name,
                source_zone,
            )
    if "remind_times" in normalized:
        reminders = normalized.get("remind_times")
        if not isinstance(reminders, list):
            raise ValueError("remind_times must be a list")
        normalized_reminders: list[str] = []
        for value in reminders:
            if value in (None, ""):
                continue
            localized = _localize_source_datetime(value, "remind_times", source_zone)
            # None 与空串已在上方过滤；成功转换必定返回非空 UTC 字符串。
            normalized_reminders.append(cast(str, localized))
        normalized["remind_times"] = normalized_reminders
    return normalized


def normalize_import_payload(
    payload: dict[str, Any],
    *,
    source_zone: ZoneInfo | None = None,
    now: datetime | None         = None,
) -> dict[str, Any]:
    """规范普通条目，统一来源时间、业务字段和不可信任的存储元数据。"""

    zone, default_timestamp = _normalization_context(source_zone, now)
    item_type = str(payload.get("type") or "").strip()
    base      = _normalize_source_datetimes(payload, _ITEM_DATETIME_FIELDS, zone)
    base.pop("_bundle_line", None)
    base.pop("version", None)
    base["type"]       = item_type
    base["created_at"] = base.get("created_at") or default_timestamp
    base["updated_at"] = base.get("updated_at") or base["created_at"]
    if item_type == "event" and not base.get("timezone"):
        base["timezone"] = zone.key
    if item_type == "task" and base.get("status") == "done":
        base["completed_at"] = base.get("completed_at") or default_timestamp
    if item_type == "task" and base.get("status") == "cancelled":
        base["cancelled_at"] = base.get("cancelled_at") or default_timestamp

    context             = base.get("context")
    attachments         = base.get("attachments")
    ai_meta             = base.get("ai_meta")
    base["context"]     = dict(context) if isinstance(context, dict) else {}
    base["attachments"] = list(attachments) if isinstance(attachments, list) else []
    base["ai_meta"]     = dict(ai_meta) if isinstance(ai_meta, dict) else {}
    base["deleted"]     = normalize_bool_flag(base.get("deleted", False))
    return cast(dict[str, Any], normalize_item_fields(base, partial=False))


def normalize_import_event_collection(
    payload: dict[str, Any],
    *,
    source_zone: ZoneInfo | None = None,
    now: datetime | None         = None,
) -> dict[str, Any]:
    """规范日程集合头，并拒绝无法安全写入集合表的字段形状。"""

    zone, default_timestamp = _normalization_context(source_zone, now)
    normalized = _normalize_source_datetimes(payload, _COLLECTION_DATETIME_FIELDS, zone)
    kind       = str(normalized.get("kind") or "").strip()
    if kind not in _COLLECTION_KINDS:
        raise ValueError("Invalid event collection kind")
    title = str(normalized.get("title") or "").strip()
    if not title:
        raise ValueError("event collection title is required")

    normalized["kind"]     = kind
    normalized["title"]    = validate_title(title)
    normalized["content"]  = sanitize_text(str(normalized.get("content") or ""), 50_000)
    normalized["category"] = validate_category(
        str(normalized.get("category") or PendoConfig.DEFAULT_CATEGORY)
    )
    normalized["location"] = validate_location(str(normalized.get("location") or ""))
    normalized["notes"]    = sanitize_text(str(normalized.get("notes") or ""), 50_000)

    raw_tags = normalized.get("tags") or []
    if not isinstance(raw_tags, list):
        raise ValueError("event collection tags must be a list")
    normalized["tags"] = list(
        dict.fromkeys(validate_tag(str(tag)) for tag in raw_tags if tag not in (None, ""))
    )

    context               = normalized.get("context")
    normalized["context"] = dict(context) if isinstance(context, dict) else {}
    visibility            = sanitize_text(str(normalized.get("visibility") or "private"), 30)
    if visibility not in _COLLECTION_VISIBILITIES:
        raise ValueError("Invalid event collection visibility")
    normalized["visibility"] = visibility

    timezone_name = sanitize_text(str(normalized.get("timezone") or zone.key), 80)
    try:
        ZoneInfo(timezone_name)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise ValueError("Invalid event collection timezone") from exc
    normalized["timezone"]       = timezone_name
    normalized["reminder_rules"] = normalize_reminder_rules(normalized.get("reminder_rules"))

    rrule                        = normalized.get("rrule")
    normalized["rrule"]          = (sanitize_text(str(rrule), 2_000) or None) if rrule else None
    source_item_id               = normalized.get("source_item_id")
    normalized["source_item_id"] = (
        (sanitize_text(str(source_item_id), 256) or None) if source_item_id else None
    )

    start_time = normalized.get("start_time")
    end_time   = normalized.get("end_time")
    if (
        start_time
        and end_time
        and datetime.fromisoformat(end_time) <= datetime.fromisoformat(start_time)
    ):
        raise ValueError("event collection end_time must be after start_time")

    normalized["created_at"] = normalized.get("created_at") or default_timestamp
    normalized["updated_at"] = normalized.get("updated_at") or normalized["created_at"]
    normalized["deleted"]    = normalize_bool_flag(normalized.get("deleted", False))
    normalized["deleted_at"] = normalized.get("deleted_at")
    normalized.pop("_bundle_line", None)
    return normalized


def inspect_bundle_bytes(
    file_bytes: bytes,
) -> tuple[ParsedBundle, list[dict[str, Any]], list[dict[str, Any]]]:
    """解析传输包，并把逐条规范化失败合并为带文件与行号的错误列表。"""

    parsed                                  = read_bundle(io.BytesIO(file_bytes))
    source                                  = cast(dict[str, Any], parsed.manifest["source"])
    source_zone                             = ZoneInfo(cast(str, source["timezone"]))
    current                                 = datetime.now(UTC)
    valid_records: list[dict[str, Any]]     = []
    valid_collections: list[dict[str, Any]] = []
    validation_errors: list[dict[str, Any]] = list(parsed.errors)
    for index, collection in enumerate(parsed.event_collections, start=1):
        try:
            valid_collections.append(
                normalize_import_event_collection(
                    collection,
                    source_zone = source_zone,
                    now         = current,
                )
            )
        except ValueError as exc:
            validation_errors.append(
                {
                    "path": EVENT_COLLECTION_FILE_NAME,
                    "line": collection.get("_bundle_line", index),
                    "type": "event_collection",
                    "message": str(exc),
                }
            )
    for item_type, records in parsed.records_by_type.items():
        for index, record in enumerate(records, start=1):
            try:
                normalized = normalize_import_payload(
                    record,
                    source_zone = source_zone,
                    now         = current,
                )
                valid_records.append(normalized)
            except ValueError as exc:
                validation_errors.append(
                    {
                        "path": TYPE_FILE_NAMES[item_type],
                        "line": record.get("_bundle_line", index),
                        "type": item_type,
                        "message": str(exc),
                    }
                )
    parsed.event_collections = valid_collections
    return parsed, valid_records, validation_errors
