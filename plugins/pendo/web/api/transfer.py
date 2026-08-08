"""Pendo 数据导出、导入预检、执行与传输审计接口。"""

from __future__ import annotations

import io
import json
import logging
import sqlite3
import uuid
from contextlib import AbstractAsyncContextManager
from datetime import date, datetime, timedelta
from typing import Annotated, Any, Final, Literal, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr, ValidationError
from starlette.concurrency import run_in_threadpool

from core.async_keyed_lock import AsyncKeyedLockPool

from ...services.db import Database, DuplicateBundleImportError
from ...utils.validators import get_item_normalizer
from ..deps import get_current_user, get_db
from ..services.bundle_import import inspect_bundle_bytes
from ..services.transfer_bundle import (
    EVENT_COLLECTION_FILE_NAME,
    EVENT_COLLECTION_TYPE,
    SUPPORTED_TYPES,
    TIME_FIELD_BY_TYPE,
    TYPE_FILE_NAMES,
    BundleRecordLimitError,
    BundleValidationError,
    ParsedBundle,
    build_manifest,
    compute_sha256,
    serialize_event_collection,
    serialize_item,
    write_bundle,
)

router = APIRouter()
logger = logging.getLogger(__name__)
_IMPORT_LOCK_POOL = AsyncKeyedLockPool(max_keys=2_048, max_key_length=256)

# 上传大小限制：100 MB；同时约束记录数量，防止小体积压缩包展开成
# 不受控的 Python/SQLite 工作量。
MAX_UPLOAD_SIZE: Final = 100 * 1024 * 1024
DEFAULT_TIMEZONE: Final = "Asia/Shanghai"

JsonObject = dict[str, Any]
ConflictPolicy = Literal["isolate", "skip", "overwrite", "duplicate"]
ImportOutcome = Literal["inserted", "updated", "skipped", "failed"]
ImportOperation = tuple[str, JsonObject]
ImportDecision = tuple[ImportOutcome, JsonObject, str]
PlannedItem = tuple[str, JsonObject, str, str]
ImportResults = dict[str, int]
ImportDetails = dict[str, list[JsonObject]]


class ExportSelection(BaseModel):  # type: ignore[misc]
    """导出类型、日期范围和解释日期时间所用的时区。"""

    model_config = ConfigDict(extra="forbid")

    types: list[str] = Field(default_factory=list)
    preset: str = Field(default="all", max_length=20)
    start: str | None = Field(default=None, max_length=10)
    end: str | None = Field(default=None, max_length=10)
    timezone: str = Field(default=DEFAULT_TIMEZONE, max_length=128)


class ExportPreviewRequest(BaseModel):  # type: ignore[misc]
    """导出预览请求。"""

    model_config = ConfigDict(extra="forbid")
    selection: ExportSelection


class ExportDownloadRequest(BaseModel):  # type: ignore[misc]
    """导出文件下载请求。"""

    model_config = ConfigDict(extra="forbid")
    selection: ExportSelection


class ImportOptionsModel(BaseModel):  # type: ignore[misc]
    """导入请求头允许的严格选项；不接受宽松布尔或未知字段。"""

    model_config = ConfigDict(extra="forbid")

    types: list[StrictStr] | None = None
    conflict_policy: ConflictPolicy = "isolate"
    invalid_policy: Literal["abort", "skip_invalid"] = "abort"
    force: StrictBool = False


def _is_unique_constraint_failure(exc: BaseException) -> bool:
    """沿有限异常链识别 SQLite 唯一约束，不解析可能变化的错误文本。"""

    current: BaseException | None = exc
    seen: set[int] = set()
    unique_codes = {
        getattr(sqlite3, "SQLITE_CONSTRAINT_PRIMARYKEY", -1),
        getattr(sqlite3, "SQLITE_CONSTRAINT_UNIQUE", -2),
    }
    for _ in range(8):
        if current is None or id(current) in seen:
            break
        seen.add(id(current))
        if isinstance(current, sqlite3.IntegrityError):
            return getattr(current, "sqlite_errorcode", None) in unique_codes
        current = current.__cause__ or current.__context__
    return False


def _get_import_lock(
    owner_id: str,
    bundle_id: str | None,
) -> AbstractAsyncContextManager[None]:
    """同一所有者和 bundle 的导入规划、事务必须串行执行。"""

    key = f"{owner_id}:{bundle_id or '__no_bundle__'}"
    return cast(AbstractAsyncContextManager[None], _IMPORT_LOCK_POOL.hold(key))


def _resolve_timezone(timezone: str | None) -> ZoneInfo:
    """解析 IANA 时区；空白值使用系统默认时区。"""

    name = str(timezone or "").strip() or DEFAULT_TIMEZONE
    if len(name) > 128:
        raise HTTPException(status_code=422, detail="Invalid timezone")
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid timezone: {name}") from exc


def resolve_range(
    selection: ExportSelection, now: datetime | None = None
) -> tuple[date | None, date | None]:
    """把导出预设解析为所选时区内的闭区间日期。"""

    zone = _resolve_timezone(selection.timezone)
    if now is not None and now.tzinfo is None:
        raise HTTPException(
            status_code=422,
            detail="Range clock must be timezone-aware; host local time is not a valid fallback",
        )
    current = now.astimezone(zone) if now else datetime.now(zone)
    today = current.date()
    quarter_month = ((today.month - 1) // 3) * 3 + 1
    last_year = today.year - 1
    preset_bounds: dict[str, tuple[date | None, date | None]] = {
        "all": (None, None),
        "week": (today - timedelta(days=today.weekday()), today),
        "month": (today.replace(day=1), today),
        "quarter": (date(today.year, quarter_month, 1), today),
        "year": (date(today.year, 1, 1), today),
        "last_year": (date(last_year, 1, 1), date(last_year, 12, 31)),
    }
    preset = selection.preset.strip()
    if preset in preset_bounds:
        return preset_bounds[preset]
    if preset != "custom":
        raise HTTPException(status_code=422, detail=f"Unsupported export preset: {preset}")
    if not selection.start or not selection.end:
        raise HTTPException(status_code=422, detail="Custom range requires start and end")
    start = _parse_date(selection.start)
    end = _parse_date(selection.end)
    if start > end:
        raise HTTPException(status_code=422, detail="Custom range start must be before end")
    return start, end


def _parse_date(value: str) -> date:
    """严格解析 ``YYYY-MM-DD`` 日期。"""

    text = value.strip()
    try:
        if len(text) != 10 or text[4] != "-" or text[7] != "-":
            raise ValueError
        return date.fromisoformat(text)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid date: {value}") from exc


def _coerce_date(value: Any, zone: ZoneInfo | None = None) -> date | None:
    """把持久化日期/日期时间转换为日期；带偏移值可先映射到目标时区。"""

    if value in (None, ""):
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        if len(text) == 10 and text[4] == "-" and text[7] == "-":
            return date.fromisoformat(text)
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if zone is not None and parsed.tzinfo is not None:
            parsed = parsed.astimezone(zone)
        return parsed.date()
    except ValueError:
        return None


def _extract_item_date(
    item: object,
    item_type: str,
    zone: ZoneInfo | None = None,
) -> date | None:
    """按条目类型选择导出范围所依据的日期字段。"""

    if item_type == "task":
        primary = (
            getattr(item, "plan_date", None)
            or getattr(item, "deadline_at", None)
            or getattr(item, "created_at", None)
        )
        return _coerce_date(primary, zone)
    field_name = TIME_FIELD_BY_TYPE[item_type]
    value = getattr(item, field_name, None)
    return _coerce_date(value, zone)


def item_matches_range(
    item: object,
    item_type: str,
    start: date | None,
    end: date | None,
    zone: ZoneInfo | None = None,
) -> bool:
    """判断条目是否与导出闭区间相交。"""

    if start is None or end is None:
        return True
    if item_type == "event":
        start_date = _coerce_date(getattr(item, "start_time", None), zone)
        end_date = _coerce_date(getattr(item, "end_time", None), zone)
        if start_date is None:
            return False
        end_date = end_date or start_date
        return start_date <= end and end_date >= start
    item_date = _extract_item_date(item, item_type, zone)
    if item_date is None:
        return False
    return start <= item_date <= end


def query_items_for_types(
    db: Database, owner_id: str, selected_types: list[str]
) -> dict[str, list[Any]]:
    """按类型完整分页读取条目，避免数据库默认页大小截断导出。"""

    result: dict[str, list[Any]] = {}
    for item_type in selected_types:
        if item_type not in SUPPORTED_TYPES:
            raise HTTPException(status_code=422, detail=f"Unsupported item type: {item_type}")
        batch_size = 1000
        offset = 0
        items: list[Any] = []
        while True:
            batch = db.get_items(
                owner_id,
                filters={"type": item_type},
                limit=batch_size,
                offset=offset,
                use_cache=False,
            )
            items.extend(batch)
            if len(batch) < batch_size:
                break
            offset += batch_size
        result[item_type] = items
    return result


def _normalize_selection(selection: ExportSelection) -> list[str]:
    """验证、去重并保留用户选择的类型顺序。"""

    normalized_types: list[str] = []
    for item_type in selection.types:
        if item_type not in SUPPORTED_TYPES:
            raise HTTPException(status_code=422, detail=f"Unsupported item type: {item_type}")
        if item_type not in normalized_types:
            normalized_types.append(item_type)
    if not normalized_types:
        raise HTTPException(status_code=422, detail="At least one type must be selected")
    return normalized_types


def _export_record_warning(record: JsonObject) -> str | None:
    """校验历史记录能否通过当前规范；导出仍保留原始序列化内容。"""

    item_type = record.get("_type")
    normalizer = get_item_normalizer(str(item_type or ""))
    if not normalizer:
        return None
    try:
        normalizer(record, True)
        return None
    except (ValueError, TypeError, KeyError):
        return f"{item_type}/{record.get('id', '?')}: 记录字段校验失败"


def _collect_event_collection_records(
    db: Database,
    owner_id: str,
    event_records: list[JsonObject],
    export_warnings: list[str],
) -> list[JsonObject]:
    """批量补齐事件图集合头，并为已丢失集合保留可定位告警。"""

    collection_sources: dict[str, JsonObject] = {}
    for record in event_records:
        collection_id = str(record.get("event_collection_id") or "").strip()
        if collection_id:
            collection_sources.setdefault(collection_id, record)
    collections = db.get_event_collections_by_ids(owner_id, list(collection_sources))
    collection_records: list[JsonObject] = []
    for collection_id, source_record in collection_sources.items():
        collection = collections.get(collection_id)
        if collection:
            collection_records.append(serialize_event_collection(collection))
        else:
            export_warnings.append(
                f"event/{source_record.get('id', '?')}: missing event collection {collection_id}"
            )
    return collection_records


def _build_export_dataset(
    db: Database, owner_id: str, selection: ExportSelection
) -> tuple[dict[str, list[JsonObject]], dict[str, int], tuple[date | None, date | None], list[str]]:
    """构建范围过滤后的导出记录、计数和兼容性告警。"""

    selected_types = _normalize_selection(selection)
    start, end = resolve_range(selection)
    zone = _resolve_timezone(selection.timezone)
    items_by_type = query_items_for_types(db, owner_id, selected_types)

    records_by_type: dict[str, list[JsonObject]] = {}
    counts: dict[str, int] = {}
    export_warnings: list[str] = []
    for item_type, items in items_by_type.items():
        matched: list[JsonObject] = []
        for item in items:
            if not item_matches_range(item, item_type, start, end, zone):
                continue
            record = serialize_item(item)
            warning = _export_record_warning(record)
            if warning:
                export_warnings.append(warning)
            matched.append(record)
        records_by_type[item_type] = matched
        counts[item_type] = len(matched)
    if "event" in records_by_type:
        collection_records = _collect_event_collection_records(
            db,
            owner_id,
            records_by_type["event"],
            export_warnings,
        )
        if collection_records:
            records_by_type[EVENT_COLLECTION_TYPE] = collection_records
            counts[EVENT_COLLECTION_TYPE] = len(collection_records)
    return records_by_type, counts, (start, end), export_warnings


def _build_bundle_bytes(
    records_by_type: dict[str, list[JsonObject]],
    selection: ExportSelection,
    start: date | None,
    end: date | None,
) -> bytes:
    """以记录本身为唯一计数真值，生成带校验清单的 ZIP。"""

    file_entries: list[JsonObject] = []
    for item_type, records in records_by_type.items():
        lines = [json.dumps(record, ensure_ascii=False) for record in records]
        content = ("\n".join(lines) + ("\n" if lines else "")).encode("utf-8")
        file_entries.append(
            {
                "path": EVENT_COLLECTION_FILE_NAME
                if item_type == EVENT_COLLECTION_TYPE
                else TYPE_FILE_NAMES[item_type],
                "type": item_type,
                "count": len(records),
                "sha256": compute_sha256(content),
            }
        )

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


def _parse_import_options(options: str | None) -> dict[str, Any]:
    """解析并严格校验导入请求头，拒绝隐式布尔转换和未知选项。"""

    if not options:
        return {}
    try:
        parsed = json.loads(options)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=422, detail="Invalid import options JSON") from exc
    if not isinstance(parsed, dict):
        raise HTTPException(status_code=422, detail="Import options must be an object")
    try:
        model = ImportOptionsModel.model_validate(parsed)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail="Invalid import options") from exc

    normalized = cast(dict[str, Any], model.model_dump(exclude_unset=True))
    if "types" in normalized:
        selected_types = normalized["types"]
        if selected_types is None:
            raise HTTPException(status_code=422, detail="Import types must be a string array")
        normalized["types"] = list(dict.fromkeys(selected_types))
    return normalized


async def _read_upload_body(request: Request) -> bytes:
    """读取请求体，并在解析压缩包前执行声明值和实际值双重限流。"""

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

    file_bytes = cast(bytes, await request.body())
    if not file_bytes:
        raise HTTPException(status_code=422, detail="Uploaded bundle is empty")
    if len(file_bytes) > MAX_UPLOAD_SIZE:
        raise HTTPException(
            status_code=413,
            detail=f"文件大小超过限制（最大 {MAX_UPLOAD_SIZE // (1024 * 1024)} MB）",
        )
    return file_bytes


def _inspect_bundle_data(
    file_bytes: bytes,
) -> tuple[ParsedBundle, list[JsonObject], list[JsonObject]]:
    """把传输包限制/格式错误转换为稳定的 HTTP 状态。"""

    try:
        return cast(
            tuple[ParsedBundle, list[JsonObject], list[JsonObject]],
            inspect_bundle_bytes(file_bytes),
        )
    except BundleRecordLimitError as exc:
        raise HTTPException(status_code=413, detail="Bundle contains too many records") from exc
    except BundleValidationError as exc:
        raise HTTPException(status_code=422, detail="导入包格式或内容校验失败") from exc


def _selected_import_types(options: dict[str, Any], parsed: ParsedBundle) -> list[str]:
    """按 bundle 可用类型过滤显式选择；未提供选择时导入全部可用类型。"""

    available = [
        summary["type"] for summary in parsed.file_summaries if summary["type"] in SUPPORTED_TYPES
    ]
    if "types" not in options:
        return available
    requested = cast(list[str], options["types"])
    selected: list[str] = []
    for item_type in requested:
        if item_type not in available:
            continue
        if item_type not in selected:
            selected.append(item_type)
    if not selected:
        raise HTTPException(
            status_code=422, detail="At least one import type must match the bundle"
        )
    return selected


def _get_item_identity(db: Database, item_id: str | None) -> dict[str, Any] | None:
    if not item_id:
        return None
    row = (
        db.get_connection()
        .execute(
            "SELECT id, owner_id, type, deleted FROM items WHERE id = ?",
            (item_id,),
        )
        .fetchone()
    )
    if not row:
        return None
    return {
        "id": row["id"],
        "owner_id": row["owner_id"],
        "type": row["type"],
        "deleted": int(row["deleted"] or 0),
    }


def _new_import_item_id(db: Database) -> str:
    while True:
        candidate = uuid.uuid4().hex
        if _get_item_identity(db, candidate) is None:
            return candidate


def _decode_import_context(value: Any) -> dict[str, Any]:
    """读取当前或历史 JSON 上下文；损坏值按无来源元数据处理。"""

    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return {}
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _index_imported_item_sources(
    db: Database,
    owner_id: str,
    item_types: set[str],
) -> dict[tuple[str, str], str]:
    """一次读取已导入来源，供 skip/overwrite 规划复用。"""

    if not item_types:
        return {}
    ordered_types = sorted(item_types)
    placeholders = ",".join("?" for _ in ordered_types)
    rows = (
        db.get_connection()
        .execute(
            f"""
            SELECT id, type, context FROM items
            WHERE owner_id = ? AND deleted = 0 AND type IN ({placeholders})
            ORDER BY updated_at DESC, id
            """,
            [owner_id, *ordered_types],
        )
        .fetchall()
    )
    sources: dict[tuple[str, str], str] = {}
    for row in rows:
        import_context = _decode_import_context(row["context"]).get("import")
        if not isinstance(import_context, dict):
            continue
        if import_context.get("policy") == "isolate":
            continue
        source_id = str(import_context.get("source_id") or "")
        if source_id:
            sources.setdefault((str(row["type"]), source_id), str(row["id"]))
    return sources


def _import_source_key(record: dict[str, Any], index: int) -> str:
    """生成导入内来源键；外部 ID 永远不直接充当数据库主键。"""

    source_id = str(record.get("id") or "").strip()
    return source_id or f"{record.get('type', 'record')}@line:{record.get('_bundle_line', index)}"


def _attach_import_metadata(
    payload: dict[str, Any],
    source_id: str,
    *,
    bundle_id: str | None,
    conflict_policy: ConflictPolicy,
) -> dict[str, Any]:
    """复制记录并附加来源、策略和隔离命名空间元数据。"""

    assigned = dict(payload)
    context = assigned.get("context")
    assigned["context"] = dict(context) if isinstance(context, dict) else {}
    import_context = dict(assigned["context"].get("import") or {})
    import_context["source_id"] = source_id
    import_context["policy"] = conflict_policy
    if conflict_policy == "isolate":
        import_context["namespace"] = bundle_id or "bundle-without-id"
    else:
        import_context.pop("namespace", None)
    assigned["context"]["import"] = import_context
    return assigned


def _assign_import_identity(
    payload: JsonObject,
    source_id: str,
    internal_id: str,
    *,
    bundle_id: str | None,
    conflict_policy: ConflictPolicy,
) -> JsonObject:
    """分配核心生成的内部 ID，并保留外部身份为普通元数据。"""

    assigned = _attach_import_metadata(
        payload,
        source_id,
        bundle_id=bundle_id,
        conflict_policy=conflict_policy,
    )
    assigned["id"] = internal_id
    return assigned


def _get_event_collection_identity(
    db: Database, collection_id: str | None
) -> dict[str, Any] | None:
    if not collection_id:
        return None
    row = (
        db.get_connection()
        .execute(
            "SELECT id, owner_id, kind, deleted FROM event_collections WHERE id = ?",
            (collection_id,),
        )
        .fetchone()
    )
    if not row:
        return None
    return {
        "id": row["id"],
        "owner_id": row["owner_id"],
        "kind": row["kind"],
        "deleted": int(row["deleted"] or 0),
    }


def _new_import_collection_id(db: Database) -> str:
    """生成未被全局集合主键占用的内部 ID。"""

    while True:
        candidate = uuid.uuid4().hex[:16]
        if _get_event_collection_identity(db, candidate) is None:
            return candidate


def _index_imported_collection_sources(
    db: Database,
    owner_id: str,
) -> dict[str, str]:
    """一次读取当前所有者的非隔离集合来源索引。"""

    rows = (
        db.get_connection()
        .execute(
            """
        SELECT id, context FROM event_collections
        WHERE owner_id = ? AND deleted = 0
        ORDER BY updated_at DESC, id
        """,
            (owner_id,),
        )
        .fetchall()
    )
    sources: dict[str, str] = {}
    for row in rows:
        import_context = _decode_import_context(row["context"]).get("import")
        if not isinstance(import_context, dict):
            continue
        if import_context.get("policy") == "isolate":
            continue
        source_id = str(import_context.get("source_id") or "")
        if source_id:
            sources.setdefault(source_id, str(row["id"]))
    return sources


def _prepare_collection_import_operations(
    db: Database,
    owner_id: str,
    collections: list[dict[str, Any]],
    *,
    selected_types: set[str],
    conflict_policy: ConflictPolicy,
    bundle_id: str | None,
) -> tuple[
    list[ImportOperation],
    dict[str, str],
    list[ImportDecision],
]:
    """规划日程集合插入/覆盖，并建立 bundle ID 到内部 ID 的映射。"""

    if "event" not in selected_types:
        return [], {}, []

    existing_sources = (
        _index_imported_collection_sources(db, owner_id)
        if conflict_policy in {"skip", "overwrite"}
        else {}
    )
    operations: list[ImportOperation] = []
    collection_id_map: dict[str, str] = {}
    decisions: list[ImportDecision] = []
    for index, collection in enumerate(collections, start=1):
        payload = dict(collection)
        original_id = _import_source_key({"type": "event_collection", **payload}, index)
        if original_id in collection_id_map:
            raise HTTPException(
                status_code=422, detail=f"Duplicate event collection source ID: {original_id}"
            )

        existing_id = existing_sources.get(original_id)
        if existing_id and conflict_policy == "skip":
            collection_id_map[original_id] = existing_id
            decisions.append(
                (
                    "skipped",
                    {"type": EVENT_COLLECTION_TYPE, **collection},
                    "同来源日程集合已存在",
                )
            )
            continue

        action = "update" if existing_id and conflict_policy == "overwrite" else "insert"
        internal_id = existing_id or _new_import_collection_id(db)
        payload = _attach_import_metadata(
            payload,
            original_id,
            bundle_id=bundle_id,
            conflict_policy=conflict_policy,
        )
        payload["id"] = internal_id
        collection_id_map[original_id] = internal_id
        operations.append((action, payload))
        if action == "update":
            decisions.append(
                (
                    "updated",
                    {"type": EVENT_COLLECTION_TYPE, **collection},
                    "已覆盖同来源日程集合",
                )
            )
        else:
            decisions.append(
                (
                    "inserted",
                    {"type": EVENT_COLLECTION_TYPE, **collection},
                    "已导入日程集合并保留来源 ID 元数据",
                )
            )

    return operations, collection_id_map, decisions


def _result_entry(record: JsonObject, reason: str | None = None) -> JsonObject:
    """生成不暴露内部主键的导入结果摘要。"""

    entry: JsonObject = {
        "type": record.get("type"),
        "id": record.get("id"),
        "title": record.get("title") or "无标题",
    }
    if reason:
        entry["reason"] = reason
    return entry


def _remap_note_references(value: object, item_id_map: dict[str, str]) -> list[JsonObject]:
    """只保留同批次可安全重写的结构化笔记引用。"""

    if not isinstance(value, list):
        return []
    references: list[JsonObject] = []
    for reference in value:
        if not isinstance(reference, dict):
            continue
        next_reference = dict(reference)
        source_id = str(next_reference.get("id") or "").strip()
        if source_id in item_id_map:
            next_reference["id"] = item_id_map[source_id]
            references.append(next_reference)
    return references


def _remap_related_item_ids(value: object, item_id_map: dict[str, str]) -> list[str]:
    """把笔记关联 ID 重写为本次导入分配的内部 ID。"""

    if not isinstance(value, list):
        return []
    return [
        item_id_map[source_id]
        for raw_id in value
        if (source_id := str(raw_id).strip()) in item_id_map
    ]


def _rewrite_import_item_relationships(
    payload: JsonObject,
    item_id_map: dict[str, str],
) -> JsonObject:
    """重写笔记关系和日程来源；丢弃无法解析到同批次内部 ID 的外部关系。"""

    if not item_id_map:
        return payload

    rewritten = dict(payload)
    if rewritten.get("type") == "note":
        if "references" in rewritten:
            rewritten["references"] = _remap_note_references(rewritten["references"], item_id_map)
        if "related_items" in rewritten:
            rewritten["related_items"] = _remap_related_item_ids(
                rewritten["related_items"], item_id_map
            )

    source_item_id = str(rewritten.get("source_item_id") or "").strip()
    if source_item_id in item_id_map:
        rewritten["source_item_id"] = item_id_map[source_item_id]
    elif "source_item_id" in rewritten:
        rewritten.pop("source_item_id", None)

    return rewritten


@router.post("/transfer/export/preview")  # type: ignore[untyped-decorator]
def preview_export(
    body: ExportPreviewRequest,
    owner_id: str = Depends(get_current_user),
    db: Database = Depends(get_db),
) -> JsonObject:
    """预览导出范围、各类型计数和历史记录兼容性告警。"""

    records_by_type, counts, (start, end), export_warnings = _build_export_dataset(
        db, owner_id, body.selection
    )
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


@router.post("/transfer/export/download")  # type: ignore[untyped-decorator]
def download_export(
    body: ExportDownloadRequest,
    owner_id: str = Depends(get_current_user),
    db: Database = Depends(get_db),
) -> Response:
    """生成传输包并在同一成功路径记录导出审计。"""

    records_by_type, counts, (start, end), export_warnings = _build_export_dataset(
        db, owner_id, body.selection
    )
    bundle_bytes = _build_bundle_bytes(records_by_type, body.selection, start, end)
    export_day = datetime.now(_resolve_timezone(body.selection.timezone)).strftime("%Y-%m-%d")
    filename = f"pendo-export-{export_day}.pendo.zip"

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


@router.post("/transfer/import/inspect")  # type: ignore[untyped-decorator]
async def inspect_import(
    request: Request,
    owner_id: str = Depends(get_current_user),
    db: Database = Depends(get_db),
) -> JsonObject:
    """在线程池解析传输包，并返回文件、错误和少量样例摘要。"""

    file_bytes = await _read_upload_body(request)
    parsed, valid_records, errors = await run_in_threadpool(_inspect_bundle_data, file_bytes)

    bundle_id = parsed.manifest.get("bundle_id")
    already_imported = (
        await run_in_threadpool(db.has_imported_bundle, owner_id, bundle_id) if bundle_id else False
    )

    sample_limit = 5
    samples = [
        {"type": record["type"], "id": record.get("id"), "title": record.get("title", "")}
        for record in valid_records[:sample_limit]
    ]
    return {
        "ok": True,
        "data": {
            "summary": {
                "types": [
                    summary["type"]
                    for summary in parsed.file_summaries
                    if summary["type"] in SUPPORTED_TYPES
                ],
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


@router.post("/transfer/import/samples")  # type: ignore[untyped-decorator]
async def import_samples(
    request: Request,
    owner_id: str = Depends(get_current_user),
    db: Database = Depends(get_db),
) -> JsonObject:
    """分页获取 bundle 样例；认证和数据库依赖仍是访问控制边界。"""

    file_bytes = await _read_upload_body(request)
    _parsed, valid_records, _errors = await run_in_threadpool(_inspect_bundle_data, file_bytes)

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
    samples = [
        {"type": r["type"], "id": r.get("id"), "title": r.get("title", "")} for r in page_records
    ]
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


def _validate_import_request(
    *,
    parsed: ParsedBundle,
    errors: list[JsonObject],
    parsed_options: dict[str, Any],
    owner_id: str,
    db: Database,
) -> tuple[set[str], str | None, ConflictPolicy]:
    """验证类型选择、bundle 幂等、无效记录策略和冲突策略。"""

    selected_types = set(_selected_import_types(parsed_options, parsed))
    raw_bundle_id = parsed.manifest.get("bundle_id")
    bundle_id = str(raw_bundle_id) if raw_bundle_id else None
    force = cast(bool, parsed_options.get("force", False))
    if bundle_id and db.has_imported_bundle(owner_id, bundle_id) and not force:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "此 bundle 已导入过，如需重新导入请勾选「强制重新导入」",
                "bundle_id": bundle_id,
            },
        )

    invalid_policy = cast(str, parsed_options.get("invalid_policy", "abort"))
    if errors and invalid_policy != "skip_invalid":
        raise HTTPException(
            status_code=422,
            detail={"errors": errors, "message": "Import validation failed"},
        )
    conflict_policy = cast(
        ConflictPolicy,
        parsed_options.get("conflict_policy", "isolate"),
    )
    return selected_types, bundle_id, conflict_policy


def _record_import_decisions(
    decisions: list[ImportDecision],
    results: ImportResults,
    details: ImportDetails,
) -> None:
    """把规划决定累计到稳定的计数和详情响应中。"""

    for outcome, record, reason in decisions:
        results[outcome] += 1
        details[outcome].append(_result_entry(record, reason))


def _plan_item_imports(
    *,
    db: Database,
    owner_id: str,
    valid_records: list[JsonObject],
    selected_types: set[str],
    conflict_policy: ConflictPolicy,
) -> tuple[list[PlannedItem], dict[str, str], list[ImportDecision]]:
    """为选中记录分配动作和内部 ID，并记录跳过决定。"""

    existing_sources = (
        _index_imported_item_sources(db, owner_id, selected_types)
        if conflict_policy in {"skip", "overwrite"}
        else {}
    )
    selected_records = [record for record in valid_records if record["type"] in selected_types]
    planned: list[PlannedItem] = []
    item_id_map: dict[str, str] = {}
    decisions: list[ImportDecision] = []
    for index, record in enumerate(selected_records, start=1):
        source_id = _import_source_key(record, index)
        if source_id in item_id_map:
            raise HTTPException(status_code=422, detail=f"Duplicate item source ID: {source_id}")

        existing_id = existing_sources.get((str(record["type"]), source_id))
        if existing_id and conflict_policy == "skip":
            item_id_map[source_id] = existing_id
            decisions.append(("skipped", record, "同类型、同来源记录已存在"))
            continue

        action = "update" if existing_id and conflict_policy == "overwrite" else "insert"
        internal_id = existing_id or _new_import_item_id(db)
        item_id_map[source_id] = internal_id
        planned.append((action, record, source_id, internal_id))
    return planned, item_id_map, decisions


def _rewrite_event_collection_reference(
    payload: JsonObject,
    collection_id_map: dict[str, str],
) -> None:
    """原地重写日程集合引用；缺失集合不能以外部 ID 泄漏入库。"""

    if payload.get("type") != "event":
        return
    source_id = str(payload.get("event_collection_id") or "").strip()
    if not source_id:
        return
    internal_id = collection_id_map.get(source_id)
    if internal_id:
        payload["event_collection_id"] = internal_id
    else:
        payload.pop("event_collection_id", None)


def _insert_reason(conflict_policy: ConflictPolicy) -> str:
    """返回新增记录对应的用户可读策略说明。"""

    return {
        "isolate": "已生成隔离命名空间内的 UUID；原 ID 仅作为来源元数据",
        "duplicate": "已生成副本 UUID；原 ID 仅作为来源元数据",
        "skip": "未发现同来源记录，已生成内部 UUID",
        "overwrite": "未发现同来源记录，已生成内部 UUID",
    }[conflict_policy]


def _build_item_import_operations(
    *,
    planned: list[PlannedItem],
    item_id_map: dict[str, str],
    collection_id_map: dict[str, str],
    bundle_id: str | None,
    conflict_policy: ConflictPolicy,
) -> tuple[list[ImportOperation], list[ImportDecision]]:
    """从规划生成入库载荷，并在内部 ID 全部分配后重写关系。"""

    operations: list[ImportOperation] = []
    decisions: list[ImportDecision] = []
    for action, record, source_id, internal_id in planned:
        payload = _assign_import_identity(
            record,
            source_id,
            internal_id,
            bundle_id=bundle_id,
            conflict_policy=conflict_policy,
        )
        _rewrite_event_collection_reference(payload, collection_id_map)
        operations.append((action, payload))
        if action == "update":
            decisions.append(("updated", record, "已覆盖同类型、同来源记录"))
        else:
            decisions.append(("inserted", record, _insert_reason(conflict_policy)))

    rewritten_operations = [
        (action, _rewrite_import_item_relationships(payload, item_id_map))
        for action, payload in operations
    ]
    return rewritten_operations, decisions


def _commit_import_plan(
    *,
    db: Database,
    owner_id: str,
    bundle_id: str | None,
    operations: list[ImportOperation],
    collection_operations: list[ImportOperation],
    filename: str | None,
    selected_types: set[str],
    results: ImportResults,
    force: bool,
) -> None:
    """原子提交完整导入计划，并把存储异常转换为稳定的公开错误。"""

    try:
        db.execute_import_bundle(
            owner_id=owner_id,
            bundle_id=bundle_id,
            operations=operations,
            filename=filename,
            types=sorted(selected_types),
            record_count=sum(results.values()),
            result_summary=results,
            force=force,
            collection_operations=collection_operations,
        )
    except DuplicateBundleImportError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "此 bundle 已导入过，如需重新导入请勾选「强制重新导入」",
                "bundle_id": bundle_id or "",
            },
        ) from exc
    except Exception as exc:
        if _is_unique_constraint_failure(exc):
            raise HTTPException(
                status_code=409,
                detail="导入记录 ID 与现有数据冲突，请选择跳过或生成副本后重试",
            ) from exc
        logger.error("Import transaction failed error_type=%s", type(exc).__name__)
        raise HTTPException(
            status_code=500,
            detail="导入事务失败，已全部回滚；请检查导入预检结果或稍后重试",
        ) from exc


def _execute_import_sync(
    *,
    parsed: ParsedBundle,
    valid_records: list[JsonObject],
    errors: list[JsonObject],
    parsed_options: dict[str, Any],
    owner_id: str,
    db: Database,
    filename: str | None,
) -> JsonObject:
    """在线程池中完成导入校验、规划、关系重写和原子提交。"""

    selected_types, bundle_id, conflict_policy = _validate_import_request(
        parsed=parsed,
        errors=errors,
        parsed_options=parsed_options,
        owner_id=owner_id,
        db=db,
    )
    results: ImportResults = {"inserted": 0, "updated": 0, "skipped": 0, "failed": 0}
    details: ImportDetails = {"inserted": [], "updated": [], "skipped": [], "failed": []}
    collection_operations, collection_id_map, collection_decisions = (
        _prepare_collection_import_operations(
            db,
            owner_id,
            parsed.event_collections,
            selected_types=selected_types,
            conflict_policy=conflict_policy,
            bundle_id=bundle_id,
        )
    )
    _record_import_decisions(collection_decisions, results, details)

    planned, item_id_map, item_decisions = _plan_item_imports(
        db=db,
        owner_id=owner_id,
        valid_records=valid_records,
        selected_types=selected_types,
        conflict_policy=conflict_policy,
    )
    operations, operation_decisions = _build_item_import_operations(
        planned=planned,
        item_id_map=item_id_map,
        collection_id_map=collection_id_map,
        bundle_id=bundle_id,
        conflict_policy=conflict_policy,
    )
    _record_import_decisions([*item_decisions, *operation_decisions], results, details)

    _commit_import_plan(
        db=db,
        owner_id=owner_id,
        bundle_id=bundle_id,
        operations=operations,
        collection_operations=collection_operations,
        filename=filename,
        selected_types=selected_types,
        results=results,
        force=cast(bool, parsed_options.get("force", False)),
    )

    return {
        "ok": True,
        "data": {
            "summary": {"types": sorted(selected_types)},
            "counts": {
                "valid": len(valid_records) + len(parsed.event_collections),
                "errors": len(errors),
            },
            "bundle_id": bundle_id,
            "warnings": parsed.warnings,
            "errors": errors,
            "results": results,
            "details": details,
        },
        "message": "",
    }


@router.post("/transfer/import/execute")  # type: ignore[untyped-decorator]
async def execute_import(
    request: Request,
    x_transfer_options: Annotated[str | None, Header(max_length=4096)] = None,
    owner_id: str = Depends(get_current_user),
    db: Database = Depends(get_db),
) -> JsonObject:
    """读取并解析 bundle，在所有者级锁内离线规划和原子提交。"""

    parsed_options = _parse_import_options(x_transfer_options)
    file_bytes = await _read_upload_body(request)
    parsed, valid_records, errors = await run_in_threadpool(_inspect_bundle_data, file_bytes)
    raw_bundle_id = parsed.manifest.get("bundle_id")
    bundle_id = str(raw_bundle_id) if raw_bundle_id else None

    async with _get_import_lock(owner_id, bundle_id):
        return cast(
            JsonObject,
            await run_in_threadpool(
                _execute_import_sync,
                parsed=parsed,
                valid_records=valid_records,
                errors=errors,
                parsed_options=parsed_options,
                owner_id=owner_id,
                db=db,
                filename=request.headers.get("x-transfer-filename"),
            ),
        )


@router.get("/transfer/logs")  # type: ignore[untyped-decorator]
def get_transfer_logs(
    owner_id: str = Depends(get_current_user),
    db: Database = Depends(get_db),
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> JsonObject:
    """分页读取当前所有者的迁移操作审计日志。"""

    if not 1 <= limit <= 100 or offset < 0:
        raise HTTPException(status_code=422, detail="Invalid transfer log pagination")
    logs = db.get_transfer_logs(owner_id, limit=limit, offset=offset)
    return {
        "ok": True,
        "data": {"logs": logs},
        "message": "",
    }
