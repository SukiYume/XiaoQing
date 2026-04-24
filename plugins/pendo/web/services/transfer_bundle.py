from __future__ import annotations

import hashlib
import io
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile

from ...models.item import Item, get_item_type_value


TRANSFER_FORMAT = "pendo-bundle"
TRANSFER_VERSION = 1
SUPPORTED_TYPES = {"event", "task", "ledger", "note", "diary"}
TYPE_FILE_NAMES = {
    "event": "data/events.ndjson",
    "task": "data/tasks.ndjson",
    "ledger": "data/ledger.ndjson",
    "note": "data/notes.ndjson",
    "diary": "data/diary.ndjson",
}
EVENT_COLLECTION_TYPE = "event_collection"
EVENT_COLLECTION_FILE_NAME = "data/event_collections.ndjson"
# 反向映射：从文件名推断类型（用于宽松导入模式）
FILE_NAME_TO_TYPE = {
    **{path: item_type for item_type, path in TYPE_FILE_NAMES.items()},
    EVENT_COLLECTION_FILE_NAME: EVENT_COLLECTION_TYPE,
}
TIME_FIELD_BY_TYPE = {
    "event": "start_time",
    "task": "due_time",
    "ledger": "ledger_date",
    "note": "created_at",
    "diary": "diary_date",
}
COMMON_FIELDS = {
    "id",
    "title",
    "content",
    "tags",
    "category",
    "created_at",
    "updated_at",
    "context",
    "visibility",
    "attachments",
    "ai_meta",
    "deleted",
    "deleted_at",
}
TYPE_FIELDS = {
    "event": {
        "start_time", "end_time", "timezone", "location", "participants",
        "remind_times", "notes",
        "event_role", "event_collection_id", "event_collection_kind", "event_index",
        "event_node_key", "source_item_id", "reminder_rules",
    },
    "task": {
        "due_time", "priority", "status", "estimate", "subtasks", "dependencies",
        "progress", "remind_times", "completed_at",
    },
    "ledger": {"amount", "direction", "ledger_category", "ledger_date", "remark"},
    "note": {"references", "last_viewed", "related_items"},
    "diary": {"mood", "mood_score", "weather", "location", "template_id", "diary_date"},
}
EVENT_COLLECTION_FIELDS = {
    "id",
    "kind",
    "title",
    "content",
    "category",
    "location",
    "tags",
    "notes",
    "context",
    "visibility",
    "timezone",
    "rrule",
    "reminder_rules",
    "start_time",
    "end_time",
    "source_item_id",
    "created_at",
    "updated_at",
    "deleted",
    "deleted_at",
}
RESERVED_IMPORT_FIELDS = {"_type", "_schema", "owner_id", "_bundle_line"}


class BundleValidationError(ValueError):
    """Raised when the bundle structure is invalid."""


@dataclass
class ParsedBundle:
    manifest: dict[str, Any]
    records_by_type: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    event_collections: list[dict[str, Any]] = field(default_factory=list)
    file_summaries: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def compute_sha256(bytes_: bytes) -> str:
    return hashlib.sha256(bytes_).hexdigest()


def build_manifest(selection: dict[str, Any], files: list[dict[str, Any]], timezone: str) -> dict[str, Any]:
    normalized_types = [item_type for item_type in selection.get("types", []) if item_type in SUPPORTED_TYPES]
    exported_at = selection.get("exported_at") or datetime.now().astimezone().isoformat(timespec="seconds")
    return {
        "format": TRANSFER_FORMAT,
        "version": TRANSFER_VERSION,
        "bundle_id": selection.get("bundle_id") or uuid.uuid4().hex,
        "exported_at": exported_at,
        "source": {"app": "pendo-web", "timezone": timezone},
        "selection": {
            "types": normalized_types,
            "preset": selection.get("preset", "all"),
            "start": selection.get("start"),
            "end": selection.get("end"),
        },
        "files": files,
        "attachments_mode": selection.get("attachments_mode", "metadata_only"),
    }


def serialize_item(item: Item | dict[str, Any]) -> dict[str, Any]:
    raw = item.to_dict() if isinstance(item, Item) else dict(item)
    item_type = get_item_type_value(raw.get("type"))
    if item_type not in SUPPORTED_TYPES:
        raise BundleValidationError(f"Unsupported item type: {item_type}")

    allowed_fields = COMMON_FIELDS | TYPE_FIELDS[item_type]
    record = {"_type": item_type, "_schema": TRANSFER_VERSION}
    for key, value in raw.items():
        if key in {"type", "owner_id"}:
            continue
        if key in allowed_fields:
            record[key] = value

    if "context" not in record or not isinstance(record["context"], dict):
        record["context"] = {}
    return record


def serialize_event_collection(collection: dict[str, Any]) -> dict[str, Any]:
    record = {"_type": EVENT_COLLECTION_TYPE, "_schema": TRANSFER_VERSION}
    for key, value in dict(collection).items():
        if key == "owner_id":
            continue
        if key in EVENT_COLLECTION_FIELDS:
            record[key] = value
    context = record.get("context")
    if not isinstance(context, dict):
        record["context"] = {}
    return record


def deserialize_record(record: dict[str, Any]) -> dict[str, Any]:
    item_type = str(record.get("_type") or "").strip()
    if item_type not in SUPPORTED_TYPES:
        raise BundleValidationError(f"Unsupported record type: {item_type}")

    payload = {"type": item_type}
    allowed_fields = COMMON_FIELDS | TYPE_FIELDS[item_type]
    extras = {}
    for key, value in record.items():
        if key in RESERVED_IMPORT_FIELDS:
            continue
        if key in allowed_fields:
            payload[key] = value
        else:
            extras[key] = value

    context = payload.get("context")
    if not isinstance(context, dict):
        context = {}
    if extras:
        context.setdefault("import", {})
        context["import"]["extra"] = extras
    payload["context"] = context
    if "_bundle_line" in record:
        payload["_bundle_line"] = record["_bundle_line"]
    return payload


def deserialize_event_collection_record(record: dict[str, Any]) -> dict[str, Any]:
    collection: dict[str, Any] = {}
    extras = {}
    for key, value in record.items():
        if key in RESERVED_IMPORT_FIELDS:
            continue
        if key in EVENT_COLLECTION_FIELDS:
            collection[key] = value
        else:
            extras[key] = value

    context = collection.get("context")
    if not isinstance(context, dict):
        context = {}
    if extras:
        context.setdefault("import", {})
        context["import"]["extra"] = extras
    collection["context"] = context
    if "_bundle_line" in record:
        collection["_bundle_line"] = record["_bundle_line"]
    return collection


def write_bundle(fileobj, manifest: dict[str, Any], typed_records: dict[str, list[dict[str, Any]]]) -> None:
    with ZipFile(fileobj, "w", compression=ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        for item_type, records in typed_records.items():
            if item_type == EVENT_COLLECTION_TYPE:
                path = EVENT_COLLECTION_FILE_NAME
            elif item_type in SUPPORTED_TYPES:
                path = TYPE_FILE_NAMES[item_type]
            else:
                raise BundleValidationError(f"Unsupported type for bundle write: {item_type}")
            lines = [json.dumps(record, ensure_ascii=False) for record in records]
            if lines:
                payload = "\n".join(lines) + "\n"
            else:
                payload = ""
            zf.writestr(path, payload.encode("utf-8"))


def read_bundle(fileobj) -> ParsedBundle:
    """Parse a .pendo.zip bundle.

    Lenient mode (for externally-constructed bundles):
    - SHA256 and count in manifest files are optional; missing/mismatched
      values produce warnings instead of hard errors.
    - Per-record ``_type`` can be omitted when it matches the file's type
      (inferred from filename).
    - Per-record ``_schema`` defaults to current TRANSFER_VERSION if absent.
    """
    fileobj.seek(0)
    with ZipFile(fileobj, "r") as zf:
        names = set(zf.namelist())
        if "manifest.json" not in names:
            raise BundleValidationError("Bundle is missing manifest.json")

        manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
        _validate_manifest(manifest)

        records_by_type: dict[str, list[dict[str, Any]]] = {item_type: [] for item_type in SUPPORTED_TYPES}
        event_collections: list[dict[str, Any]] = []
        file_summaries: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        warnings: list[str] = []

        for entry in manifest.get("files", []):
            path = entry["path"]
            item_type = entry.get("type") or FILE_NAME_TO_TYPE.get(path)
            if not item_type or (item_type not in SUPPORTED_TYPES and item_type != EVENT_COLLECTION_TYPE):
                raise BundleValidationError(f"Unsupported file type in manifest: {item_type}")
            expected_path = EVENT_COLLECTION_FILE_NAME if item_type == EVENT_COLLECTION_TYPE else TYPE_FILE_NAMES[item_type]
            if path != expected_path:
                raise BundleValidationError(f"Unexpected path for {item_type}: {path}")
            if path not in names:
                raise BundleValidationError(f"Bundle is missing {path}")

            file_bytes = zf.read(path)

            # SHA256: 有则校验，无则警告
            expected_sha = entry.get("sha256")
            if expected_sha:
                digest = compute_sha256(file_bytes)
                if digest != expected_sha:
                    raise BundleValidationError(f"Checksum mismatch for {path}")
            else:
                warnings.append(f"{path}: 缺少 sha256 校验和，跳过完整性检查")

            line_count, valid_count = 0, 0
            for index, line in enumerate(file_bytes.decode("utf-8").splitlines(), start=1):
                if not line.strip():
                    continue
                line_count += 1
                try:
                    raw_record = json.loads(line)

                    # _type: 有则校验，无则从文件名推断
                    record_type = raw_record.get("_type")
                    if record_type is None:
                        raw_record["_type"] = item_type
                    elif record_type != item_type:
                        raise BundleValidationError(f"Record type mismatch in {path}:{index}")

                    # _schema: 有则校验，无则默认当前版本
                    record_schema = raw_record.get("_schema")
                    if record_schema is None:
                        raw_record["_schema"] = TRANSFER_VERSION
                    elif record_schema != TRANSFER_VERSION:
                        raise BundleValidationError(f"Unsupported schema in {path}:{index}")

                    raw_record["_bundle_line"] = index
                    if item_type == EVENT_COLLECTION_TYPE:
                        payload = deserialize_event_collection_record(raw_record)
                        event_collections.append(payload)
                    else:
                        payload = deserialize_record(raw_record)
                        records_by_type[item_type].append(payload)
                    valid_count += 1
                except (json.JSONDecodeError, BundleValidationError, TypeError, ValueError) as exc:
                    errors.append({
                        "path": path,
                        "line": index,
                        "type": item_type,
                        "message": str(exc),
                    })

            # count: 有则校验，无则警告
            expected_count = entry.get("count")
            if expected_count is not None and line_count != expected_count:
                raise BundleValidationError(f"Count mismatch for {path}")
            elif expected_count is None:
                warnings.append(f"{path}: 缺少 count 字段，跳过行数校验")

            file_summaries.append({
                "path": path,
                "type": item_type,
                "count": line_count,
                "valid": valid_count,
            })

        return ParsedBundle(
            manifest=manifest,
            records_by_type={k: v for k, v in records_by_type.items() if v},
            event_collections=event_collections,
            file_summaries=file_summaries,
            errors=errors,
            warnings=warnings,
        )


def _validate_manifest(manifest: dict[str, Any]) -> None:
    if manifest.get("format") != TRANSFER_FORMAT:
        raise BundleValidationError("Unsupported bundle format")
    if manifest.get("version") != TRANSFER_VERSION:
        raise BundleValidationError("Unsupported bundle version")
    if not isinstance(manifest.get("files"), list):
        raise BundleValidationError("Bundle manifest files must be a list")
    if not isinstance(manifest.get("selection"), dict):
        raise BundleValidationError("Bundle manifest selection must be an object")
