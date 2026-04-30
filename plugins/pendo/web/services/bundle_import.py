from __future__ import annotations

import io
from datetime import datetime
from typing import Any

from ...utils.validators import normalize_item_fields
from .transfer_bundle import BundleValidationError, EVENT_COLLECTION_FILE_NAME, TYPE_FILE_NAMES, read_bundle


def normalize_import_payload(payload: dict[str, Any]) -> dict[str, Any]:
    item_type = payload["type"]
    base = dict(payload)
    base.pop("_bundle_line", None)
    normalized = normalize_item_fields(base, partial=False)
    normalized["type"] = item_type
    if "id" in payload:
        normalized["id"] = payload["id"]
    normalized["created_at"] = payload.get("created_at") or datetime.now().isoformat(timespec="seconds")
    normalized["updated_at"] = payload.get("updated_at") or normalized["created_at"]
    normalized["context"] = payload.get("context") if isinstance(payload.get("context"), dict) else {}
    normalized["attachments"] = payload.get("attachments") if isinstance(payload.get("attachments"), list) else []
    normalized["ai_meta"] = payload.get("ai_meta") if isinstance(payload.get("ai_meta"), dict) else {}
    normalized["deleted"] = bool(payload.get("deleted", False))
    normalized["deleted_at"] = payload.get("deleted_at")
    normalized.pop("_bundle_line", None)
    return normalized


def normalize_import_event_collection(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(payload)
    kind = str(normalized.get("kind") or "").strip()
    if kind not in {"multi_node", "recurring"}:
        raise ValueError("Invalid event collection kind")
    title = str(normalized.get("title") or "").strip()
    if not title:
        raise ValueError("event collection title is required")

    normalized["kind"] = kind
    normalized["title"] = title
    normalized.setdefault("content", "")
    normalized.setdefault("category", "未分类")
    normalized.setdefault("location", "")
    normalized.setdefault("tags", [])
    normalized.setdefault("notes", "")
    normalized["context"] = normalized.get("context") if isinstance(normalized.get("context"), dict) else {}
    normalized.setdefault("visibility", "private")
    normalized.setdefault("timezone", "Asia/Shanghai")
    normalized.setdefault("reminder_rules", [])
    normalized["created_at"] = normalized.get("created_at") or datetime.now().isoformat(timespec="seconds")
    normalized["updated_at"] = normalized.get("updated_at") or normalized["created_at"]
    normalized["deleted"] = bool(normalized.get("deleted", False))
    normalized["deleted_at"] = normalized.get("deleted_at")
    normalized.pop("_bundle_line", None)
    return normalized


def inspect_bundle_bytes(file_bytes: bytes) -> tuple[Any, list[dict[str, Any]], list[dict[str, Any]]]:
    parsed = read_bundle(io.BytesIO(file_bytes))
    valid_records = []
    valid_collections = []
    validation_errors = list(parsed.errors)
    for index, collection in enumerate(parsed.event_collections, start=1):
        try:
            valid_collections.append(normalize_import_event_collection(collection))
        except ValueError as exc:
            validation_errors.append({
                "path": EVENT_COLLECTION_FILE_NAME,
                "line": collection.get("_bundle_line", index),
                "type": "event_collection",
                "message": str(exc),
            })
    for item_type, records in parsed.records_by_type.items():
        for index, record in enumerate(records, start=1):
            try:
                normalized = normalize_import_payload(record)
                valid_records.append(normalized)
            except ValueError as exc:
                validation_errors.append({
                    "path": TYPE_FILE_NAMES[item_type],
                    "line": record.get("_bundle_line", index),
                    "type": item_type,
                    "message": str(exc),
                })
    parsed.event_collections = valid_collections
    return parsed, valid_records, validation_errors
