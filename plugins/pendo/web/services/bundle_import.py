from __future__ import annotations

import io
from datetime import datetime
from typing import Any

from ...utils.validators import (
    normalize_diary_fields,
    normalize_event_fields,
    normalize_ledger_fields,
    normalize_note_fields,
    normalize_task_fields,
)
from .transfer_bundle import BundleValidationError, TYPE_FILE_NAMES, read_bundle


_NORMALIZER_MAP = {
    "event": normalize_event_fields,
    "task": normalize_task_fields,
    "note": normalize_note_fields,
    "diary": normalize_diary_fields,
    "ledger": normalize_ledger_fields,
}


def normalize_import_payload(payload: dict[str, Any]) -> dict[str, Any]:
    item_type = payload["type"]
    base = dict(payload)
    preserved = {
        key: value for key, value in base.items()
        if key not in {"type", "id", "created_at", "updated_at", "context", "attachments", "ai_meta", "deleted", "deleted_at", "_bundle_line"}
    }

    normalizer = _NORMALIZER_MAP.get(item_type)
    if not normalizer:
        raise ValueError(f"Unsupported record type: {item_type}")
    normalized = normalizer(base, partial=False)

    for key, value in preserved.items():
        normalized.setdefault(key, value)
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


def inspect_bundle_bytes(file_bytes: bytes) -> tuple[Any, list[dict[str, Any]], list[dict[str, Any]]]:
    parsed = read_bundle(io.BytesIO(file_bytes))
    valid_records = []
    validation_errors = list(parsed.errors)
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
    return parsed, valid_records, validation_errors
