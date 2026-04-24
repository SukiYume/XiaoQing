from __future__ import annotations

import io
import json
import uuid
import asyncio
from datetime import date, datetime, timedelta
from typing import Any, Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from ...models.item import get_item_type_value
from ...services.db import Database, DuplicateBundleImportError
from ...utils.validators import (
    normalize_diary_fields,
    normalize_event_fields,
    normalize_ledger_fields,
    normalize_note_fields,
    normalize_task_fields,
)
from ..deps import get_current_user, get_db
from ..services.bundle_import import inspect_bundle_bytes, normalize_import_payload
from ..services.transfer_bundle import (
    BundleValidationError,
    EVENT_COLLECTION_FILE_NAME,
    EVENT_COLLECTION_TYPE,
    SUPPORTED_TYPES,
    TIME_FIELD_BY_TYPE,
    build_manifest,
    read_bundle,
    serialize_event_collection,
    serialize_item,
    write_bundle,
    compute_sha256,
    TYPE_FILE_NAMES,
)


router = APIRouter()
_IMPORT_BUNDLE_LOCKS: dict[str, asyncio.Lock] = {}


def _get_import_lock(owner_id: str, bundle_id: str | None) -> asyncio.Lock:
    key = f"{owner_id}:{bundle_id or '__no_bundle__'}"
    lock = _IMPORT_BUNDLE_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _IMPORT_BUNDLE_LOCKS[key] = lock
    return lock

# 上传大小限制：100 MB
MAX_UPLOAD_SIZE = 100 * 1024 * 1024

_NORMALIZER_MAP = {
    "event": normalize_event_fields,
    "task": normalize_task_fields,
    "note": normalize_note_fields,
    "diary": normalize_diary_fields,
    "ledger": normalize_ledger_fields,
}


class ExportSelection(BaseModel):
    types: list[str] = Field(default_factory=list)
    preset: str = "all"
    start: Optional[str] = None
    end: Optional[str] = None
    timezone: str = "Asia/Shanghai"


class ExportPreviewRequest(BaseModel):
    selection: ExportSelection


class ExportDownloadRequest(BaseModel):
    selection: ExportSelection


def resolve_range(selection: ExportSelection, now: Optional[datetime] = None) -> tuple[Optional[date], Optional[date]]:
    zone = ZoneInfo(selection.timezone or "Asia/Shanghai")
    current = now.astimezone(zone) if now else datetime.now(zone)
    today = current.date()
    if selection.preset == "all":
        return None, None
    if selection.preset == "week":
        start = today - timedelta(days=today.weekday())
        return start, today
    if selection.preset == "month":
        start = today.replace(day=1)
        return start, today
    if selection.preset == "quarter":
        quarter_month = ((today.month - 1) // 3) * 3 + 1
        return date(today.year, quarter_month, 1), today
    if selection.preset == "year":
        return date(today.year, 1, 1), today
    if selection.preset == "last_year":
        last_year = today.year - 1
        return date(last_year, 1, 1), date(last_year, 12, 31)
    if selection.preset == "custom":
        if not selection.start or not selection.end:
            raise HTTPException(status_code=422, detail="Custom range requires start and end")
        start = _parse_date(selection.start)
        end = _parse_date(selection.end)
        if start > end:
            raise HTTPException(status_code=422, detail="Custom range start must be before end")
        return start, end
    raise HTTPException(status_code=422, detail=f"Unsupported export preset: {selection.preset}")


def _parse_date(value: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid date: {value}") from exc


def _coerce_date(value: Any) -> Optional[date]:
    if value in (None, ""):
        return None
    text = str(value)
    try:
        if len(text) == 10:
            return datetime.strptime(text, "%Y-%m-%d").date()
        return datetime.fromisoformat(text).date()
    except ValueError:
        return None


def _coerce_date_tz(value: Any, zone: ZoneInfo) -> Optional[date]:
    """将日期时间字符串转换为指定时区的 date 对象"""
    if value in (None, ""):
        return None
    text = str(value)
    try:
        if len(text) == 10:
            return datetime.strptime(text, "%Y-%m-%d").date()
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is not None:
            return dt.astimezone(zone).date()
        return dt.date()
    except ValueError:
        return None


def _extract_item_date(item, item_type: str, zone: Optional[ZoneInfo] = None) -> Optional[date]:
    if item_type == "task":
        primary = getattr(item, "due_time", None) or getattr(item, "created_at", None)
        return _coerce_date_tz(primary, zone) if zone else _coerce_date(primary)
    field_name = TIME_FIELD_BY_TYPE[item_type]
    value = getattr(item, field_name, None)
    return _coerce_date_tz(value, zone) if zone else _coerce_date(value)


def item_matches_range(item, item_type: str, start: Optional[date], end: Optional[date], zone: Optional[ZoneInfo] = None) -> bool:
    if start is None or end is None:
        return True
    item_date = _extract_item_date(item, item_type, zone)
    if item_date is None:
        return False
    return start <= item_date <= end


def query_items_for_types(db: Database, owner_id: str, selected_types: list[str]) -> dict[str, list[Any]]:
    result: dict[str, list[Any]] = {}
    for item_type in selected_types:
        if item_type not in SUPPORTED_TYPES:
            raise HTTPException(status_code=422, detail=f"Unsupported item type: {item_type}")
        batch_size = 1000
        offset = 0
        items: list[Any] = []
        while True:
            batch = db.get_items(owner_id, filters={"type": item_type}, limit=batch_size, offset=offset)
            items.extend(batch)
            if len(batch) < batch_size:
                break
            offset += batch_size
        result[item_type] = items
    return result


def _normalize_selection(selection: ExportSelection) -> list[str]:
    normalized_types = []
    for item_type in selection.types:
        if item_type not in SUPPORTED_TYPES:
            raise HTTPException(status_code=422, detail=f"Unsupported item type: {item_type}")
        if item_type not in normalized_types:
            normalized_types.append(item_type)
    if not normalized_types:
        raise HTTPException(status_code=422, detail="At least one type must be selected")
    return normalized_types


def _validate_export_record(record: dict[str, Any]) -> tuple[dict[str, Any], Optional[str]]:
    """导出时对记录做规范化校验，返回 (record, warning_or_none)"""
    item_type = record.get("_type")
    normalizer = _NORMALIZER_MAP.get(item_type)
    if not normalizer:
        return record, None
    try:
        normalizer(record, partial=True)
        return record, None
    except (ValueError, TypeError, KeyError) as exc:
        return record, f"{item_type}/{record.get('id', '?')}: {exc}"


def _build_export_dataset(db: Database, owner_id: str, selection: ExportSelection) -> tuple[dict[str, list[dict[str, Any]]], dict[str, int], tuple[Optional[date], Optional[date]], list[str]]:
    selected_types = _normalize_selection(selection)
    start, end = resolve_range(selection)
    zone = ZoneInfo(selection.timezone or "Asia/Shanghai")
    items_by_type = query_items_for_types(db, owner_id, selected_types)

    records_by_type: dict[str, list[dict[str, Any]]] = {}
    counts: dict[str, int] = {}
    export_warnings: list[str] = []
    for item_type, items in items_by_type.items():
        matched = []
        for item in items:
            if not item_matches_range(item, item_type, start, end, zone):
                continue
            record = serialize_item(item)
            record, warning = _validate_export_record(record)
            if warning:
                export_warnings.append(warning)
            matched.append(record)
        records_by_type[item_type] = matched
        counts[item_type] = len(matched)
    if "event" in records_by_type:
        seen_collection_ids: set[str] = set()
        collection_records: list[dict[str, Any]] = []
        for record in records_by_type["event"]:
            collection_id = str(record.get("event_collection_id") or "").strip()
            if not collection_id or collection_id in seen_collection_ids:
                continue
            collection = db.get_event_collection(collection_id, owner_id)
            if not collection:
                export_warnings.append(f"event/{record.get('id', '?')}: missing event collection {collection_id}")
                continue
            collection_records.append(serialize_event_collection(collection))
            seen_collection_ids.add(collection_id)
        if collection_records:
            records_by_type[EVENT_COLLECTION_TYPE] = collection_records
            counts[EVENT_COLLECTION_TYPE] = len(collection_records)
    return records_by_type, counts, (start, end), export_warnings


def _build_bundle_bytes(records_by_type: dict[str, list[dict[str, Any]]], selection: ExportSelection, counts: dict[str, int], start: Optional[date], end: Optional[date]) -> bytes:
    file_entries = []
    for item_type, records in records_by_type.items():
        lines = [json.dumps(record, ensure_ascii=False) for record in records]
        content = ("\n".join(lines) + ("\n" if lines else "")).encode("utf-8")
        file_entries.append({
            "path": EVENT_COLLECTION_FILE_NAME if item_type == EVENT_COLLECTION_TYPE else TYPE_FILE_NAMES[item_type],
            "type": item_type,
            "count": counts[item_type],
            "sha256": compute_sha256(content),
        })

    manifest = build_manifest(
        {
            "types": list(records_by_type.keys()),
            "preset": selection.preset,
            "start": start.isoformat() if start else None,
            "end": end.isoformat() if end else None,
        },
        file_entries,
        selection.timezone,
    )
    buffer = io.BytesIO()
    write_bundle(buffer, manifest, records_by_type)
    return buffer.getvalue()


def _parse_import_options(options: Optional[str]) -> dict[str, Any]:
    if not options:
        return {}
    try:
        parsed = json.loads(options)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=422, detail="Invalid import options JSON") from exc
    if not isinstance(parsed, dict):
        raise HTTPException(status_code=422, detail="Import options must be an object")
    return parsed


async def _read_upload_body(request: Request) -> bytes:
    """读取请求体，检查大小限制"""
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            size = int(content_length)
        except (ValueError, TypeError):
            size = 0
        if size > MAX_UPLOAD_SIZE:
            raise HTTPException(
                status_code=413,
                detail=f"文件大小超过限制（最大 {MAX_UPLOAD_SIZE // (1024 * 1024)} MB）",
            )

    file_bytes = await request.body()
    if not file_bytes:
        raise HTTPException(status_code=422, detail="Uploaded bundle is empty")
    if len(file_bytes) > MAX_UPLOAD_SIZE:
        raise HTTPException(
            status_code=413,
            detail=f"文件大小超过限制（最大 {MAX_UPLOAD_SIZE // (1024 * 1024)} MB）",
        )
    return file_bytes


def _inspect_bundle_data(file_bytes: bytes) -> tuple[Any, list[dict[str, Any]], list[dict[str, Any]]]:
    try:
        return inspect_bundle_bytes(file_bytes)
    except BundleValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _normalize_import_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return normalize_import_payload(payload)


def _selected_import_types(options: dict[str, Any], parsed) -> list[str]:
    requested = options.get("types")
    available = [
        summary["type"]
        for summary in parsed.file_summaries
        if summary["type"] in SUPPORTED_TYPES
    ]
    if not requested:
        return available
    selected = []
    for item_type in requested:
        if item_type not in available:
            continue
        if item_type not in selected:
            selected.append(item_type)
    if not selected:
        raise HTTPException(status_code=422, detail="At least one import type must match the bundle")
    return selected


def _new_import_collection_id(db: Database, owner_id: str) -> str:
    while True:
        candidate = uuid.uuid4().hex[:16]
        if not db.get_event_collection(candidate, owner_id):
            return candidate


def _prepare_collection_import_operations(
    db: Database,
    owner_id: str,
    collections: list[dict[str, Any]],
    *,
    selected_types: set[str],
    conflict_policy: str,
) -> tuple[list[tuple[str, dict[str, Any]]], dict[str, str]]:
    if "event" not in selected_types:
        return [], {}

    operations: list[tuple[str, dict[str, Any]]] = []
    collection_id_map: dict[str, str] = {}
    for collection in collections:
        payload = dict(collection)
        original_id = str(payload.get("id") or "").strip()
        if not original_id:
            original_id = _new_import_collection_id(db, owner_id)
            payload["id"] = original_id

        existing = db.get_event_collection(original_id, owner_id)
        if existing and str(existing.get("kind") or "") != str(payload.get("kind") or ""):
            raise HTTPException(
                status_code=422,
                detail=f"Event collection kind mismatch for {original_id}",
            )

        if existing and conflict_policy == "overwrite":
            collection_id_map[original_id] = original_id
            operations.append(("update", payload))
            continue
        if existing and conflict_policy == "duplicate":
            duplicate_id = _new_import_collection_id(db, owner_id)
            payload["id"] = duplicate_id
            payload.setdefault("context", {})
            context_import = payload["context"].get("import", {})
            context_import["source_id"] = original_id
            payload["context"]["import"] = context_import
            collection_id_map[original_id] = duplicate_id
            operations.append(("insert", payload))
            continue

        collection_id_map[original_id] = original_id
        if not existing:
            operations.append(("insert", payload))

    return operations, collection_id_map


def _result_entry(record: dict[str, Any], reason: Optional[str] = None) -> dict[str, Any]:
    entry = {
        "type": record.get("type"),
        "id": record.get("id"),
        "title": record.get("title") or "无标题",
    }
    if reason:
        entry["reason"] = reason
    return entry


@router.post("/transfer/export/preview")
def preview_export(
    body: ExportPreviewRequest,
    owner_id: str = Depends(get_current_user),
    db: Database = Depends(get_db),
):
    records_by_type, counts, (start, end), export_warnings = _build_export_dataset(db, owner_id, body.selection)
    return {
        "ok": True,
        "data": {
            "selection": {
                "types": list(records_by_type.keys()),
                "preset": body.selection.preset,
                "start": start.isoformat() if start else None,
                "end": end.isoformat() if end else None,
            },
            "counts": counts,
            "total": sum(counts.values()),
            "warnings": export_warnings,
        },
        "message": "",
    }


@router.post("/transfer/export/download")
def download_export(
    body: ExportDownloadRequest,
    owner_id: str = Depends(get_current_user),
    db: Database = Depends(get_db),
):
    records_by_type, counts, (start, end), export_warnings = _build_export_dataset(db, owner_id, body.selection)
    bundle_bytes = _build_bundle_bytes(records_by_type, body.selection, counts, start, end)
    filename = f"pendo-export-{datetime.now().strftime('%Y-%m-%d')}.pendo.zip"

    total = sum(counts.values())
    db.log_transfer(
        owner_id=owner_id,
        action="export",
        filename=filename,
        types=list(records_by_type.keys()),
        record_count=total,
        result_summary={"counts": counts, "warnings_count": len(export_warnings)},
    )

    return Response(
        content=bundle_bytes,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/transfer/import/inspect")
async def inspect_import(
    request: Request,
    owner_id: str = Depends(get_current_user),
    db: Database = Depends(get_db),
):
    file_bytes = await _read_upload_body(request)
    parsed, valid_records, errors = _inspect_bundle_data(file_bytes)

    bundle_id = parsed.manifest.get("bundle_id")
    already_imported = db.has_imported_bundle(owner_id, bundle_id) if bundle_id else False

    sample_limit = 5
    samples = [{"type": record["type"], "id": record.get("id"), "title": record.get("title", "")} for record in valid_records[:sample_limit]]
    return {
        "ok": True,
        "data": {
            "summary": {
                "types": [summary["type"] for summary in parsed.file_summaries if summary["type"] in SUPPORTED_TYPES],
                "files": len(parsed.file_summaries),
            },
            "files": parsed.file_summaries,
            "counts": {
                "valid": len(valid_records) + len(parsed.event_collections),
                "errors": len(errors),
                "total_samples": len(valid_records),
            },
            "bundle_id": bundle_id,
            "already_imported": already_imported,
            "warnings": parsed.warnings,
            "errors": errors,
            "samples": samples,
        },
        "message": "",
    }


@router.post("/transfer/import/samples")
async def import_samples(
    request: Request,
    owner_id: str = Depends(get_current_user),
    db: Database = Depends(get_db),
):
    """分页获取 bundle 中的样例记录"""
    file_bytes = await _read_upload_body(request)
    parsed, valid_records, _errors = _inspect_bundle_data(file_bytes)

    page_str = request.headers.get("x-transfer-page", "1")
    page_size_str = request.headers.get("x-transfer-page-size", "20")
    try:
        page = max(1, int(page_str))
        page_size = max(1, min(100, int(page_size_str)))
    except (ValueError, TypeError):
        page, page_size = 1, 20

    start_idx = (page - 1) * page_size
    end_idx = start_idx + page_size
    page_records = valid_records[start_idx:end_idx]
    samples = [{"type": r["type"], "id": r.get("id"), "title": r.get("title", "")} for r in page_records]
    return {
        "ok": True,
        "data": {
            "samples": samples,
            "page": page,
            "page_size": page_size,
            "total": len(valid_records),
        },
        "message": "",
    }


@router.post("/transfer/import/execute")
async def execute_import(
    request: Request,
    x_transfer_options: Optional[str] = Header(default=None),
    owner_id: str = Depends(get_current_user),
    db: Database = Depends(get_db),
):
    parsed_options = _parse_import_options(x_transfer_options)
    file_bytes = await _read_upload_body(request)
    parsed, valid_records, errors = _inspect_bundle_data(file_bytes)
    selected_types = set(_selected_import_types(parsed_options, parsed))

    bundle_id = parsed.manifest.get("bundle_id")

    async with _get_import_lock(owner_id, bundle_id):
        # 幂等性检查
        if bundle_id and db.has_imported_bundle(owner_id, bundle_id):
            force = parsed_options.get("force", False)
            if not force:
                raise HTTPException(
                    status_code=409,
                    detail={"message": "此 bundle 已导入过，如需重新导入请勾选「强制重新导入」", "bundle_id": bundle_id},
                )

        invalid_policy = parsed_options.get("invalid_policy", "abort")
        if errors and invalid_policy != "skip_invalid":
            raise HTTPException(status_code=422, detail={"errors": errors, "message": "Import validation failed"})

        conflict_policy = parsed_options.get("conflict_policy", "skip")
        if conflict_policy not in {"skip", "overwrite", "duplicate"}:
            raise HTTPException(status_code=422, detail="Unsupported conflict policy")

        results = {"inserted": 0, "updated": 0, "skipped": 0, "failed": 0}
        details: dict[str, list] = {"inserted": [], "updated": [], "skipped": [], "failed": []}
        collection_operations, collection_id_map = _prepare_collection_import_operations(
            db,
            owner_id,
            parsed.event_collections,
            selected_types=selected_types,
            conflict_policy=conflict_policy,
        )

        # 构建批量操作列表（在事务前完成决策）
        operations: list[tuple[str, dict[str, Any]]] = []
        for record in valid_records:
            if record["type"] not in selected_types:
                continue

            item_id = record.get("id")
            existing = db.get_item(item_id, owner_id=owner_id) if item_id else None
            payload = dict(record)
            if payload.get("type") == "event":
                collection_id = str(payload.get("event_collection_id") or "").strip()
                if collection_id and collection_id in collection_id_map:
                    payload["event_collection_id"] = collection_id_map[collection_id]

            if existing:
                existing_type = str(getattr(existing.type, "value", existing.type))
                incoming_type = str(record.get("type", "") or "")
                if existing_type != incoming_type:
                    results["failed"] += 1
                    details["failed"].append(
                        _result_entry(
                            record,
                            f"同 ID 现有条目类型为 {existing_type}，导入类型为 {incoming_type}，拒绝覆盖",
                        )
                    )
                    continue
                if conflict_policy == "skip":
                    results["skipped"] += 1
                    details["skipped"].append(_result_entry(record, "ID 已存在，按策略跳过"))
                    continue
                if conflict_policy == "overwrite":
                    operations.append(("update", payload))
                    results["updated"] += 1
                    details["updated"].append(_result_entry(record))
                    continue
                # duplicate: 生成足够长的 ID 避免碰撞 (16 hex = 64 bit)
                original_id = item_id
                payload.pop("id", None)
                payload["id"] = uuid.uuid4().hex[:16]
                payload.setdefault("context", {})
                ctx_import = payload["context"].get("import", {})
                ctx_import["source_id"] = original_id
                payload["context"]["import"] = ctx_import
                operations.append(("insert", payload))
                results["inserted"] += 1
                details["inserted"].append(_result_entry(record, "已生成副本，保留原始 source_id"))
                continue

            # 新记录插入
            if not item_id:
                payload["id"] = uuid.uuid4().hex[:16]
            operations.append(("insert", payload))
            results["inserted"] += 1
            details["inserted"].append(_result_entry(record))

        total_processed = results["inserted"] + results["updated"] + results["skipped"] + results["failed"]
        try:
            db.execute_import_bundle(
                owner_id=owner_id,
                bundle_id=bundle_id,
                operations=operations,
                filename=request.headers.get("x-transfer-filename"),
                types=sorted(selected_types),
                record_count=total_processed,
                result_summary=results,
                force=bool(parsed_options.get("force", False)),
                collection_operations=collection_operations,
            )
        except DuplicateBundleImportError as exc:
            raise HTTPException(
                status_code=409,
                detail={"message": "此 bundle 已导入过，如需重新导入请勾选「强制重新导入」", "bundle_id": str(exc)},
            ) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail=f"导入事务失败，已全部回滚：{exc}",
            ) from exc

        return {
            "ok": True,
            "data": {
                "summary": {"types": sorted(selected_types)},
                "counts": {"valid": len(valid_records), "errors": len(errors)},
                "bundle_id": bundle_id,
                "warnings": parsed.warnings,
                "errors": errors,
                "results": results,
                "details": details,
            },
            "message": "",
        }


@router.get("/transfer/logs")
def get_transfer_logs(
    owner_id: str = Depends(get_current_user),
    db: Database = Depends(get_db),
    limit: int = 50,
    offset: int = 0,
):
    """获取迁移操作审计日志"""
    logs = db.get_transfer_logs(owner_id, limit=min(limit, 100), offset=offset)
    return {
        "ok": True,
        "data": {"logs": logs},
        "message": "",
    }
